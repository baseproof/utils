// hubd — WireGuard tunnel control plane.
//
// Runs on the GCP hub. Issues identity (client id, tunnel IP, pre-shared key)
// to enrolling clients, programs the kernel WireGuard device live via netlink,
// and tracks liveness from real handshake timestamps.
//
// Clients generate their own keypair and send only the public half. This
// service never sees, transmits, or stores a client private key.
//
//	go mod init hubd
//	go get golang.zx2c4.com/wireguard/wgctrl modernc.org/sqlite
//	go build -o hubd .
//
//	./hubd token --class egress --note "mac mini, garage"
//	./hubd serve --endpoint hub.example.com:51820
//
// Public surface is POST /v1/enroll only. Everything else requires a source
// address inside the tunnel CIDR plus the client's bearer token.
package main

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.zx2c4.com/wireguard/wgctrl"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
	_ "modernc.org/sqlite"
)

const (
	handshakeStale = 3 * time.Minute
	defaultJoinTTL = 720 * time.Hour
	keepalive      = 25 * time.Second
)

type Hub struct {
	db       *sql.DB
	wg       *wgctrl.Client
	device   string
	endpoint string // hostname:port advertised to clients
	cidr     *net.IPNet
	hubIP    net.IP
	hubPub   string
	proxyPt  int
	joinKey  []byte     // the single enrollment key
	mu       sync.Mutex // serialises IP allocation
}

// ---------------------------------------------------------------- schema

const schema = `
CREATE TABLE IF NOT EXISTS peers (
  client_id   TEXT PRIMARY KEY,
  class       TEXT NOT NULL CHECK (class IN ('egress','consumer')),
  hostname    TEXT NOT NULL,
  pubkey      TEXT NOT NULL UNIQUE,
  psk         TEXT NOT NULL,
  ip          TEXT NOT NULL UNIQUE,
  auth_hash   TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  revoked_at  INTEGER
);
CREATE INDEX IF NOT EXISTS peers_class ON peers(class) WHERE revoked_at IS NULL;
`

// ------------------------------------------------- enrollment key + join tokens

// The hub holds exactly one enrollment key. Per-node join tokens are HMACs
// derived from it, so nothing about them is stored server-side: verification
// is a recomputation. Rotating the key invalidates every outstanding token.
func loadOrCreateKey(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if err == nil {
		k, derr := base64.StdEncoding.DecodeString(strings.TrimSpace(string(b)))
		if derr != nil || len(k) < 32 {
			return nil, errors.New("enrollment key malformed — expected 32+ bytes, base64")
		}
		return k, nil
	}
	if !os.IsNotExist(err) {
		return nil, err
	}
	k := make([]byte, 32)
	if _, err := rand.Read(k); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, []byte(base64.StdEncoding.EncodeToString(k)+"\n"), 0o600); err != nil {
		return nil, err
	}
	log.Printf("generated enrollment key at %s", path)
	return k, nil
}

type joinClaims struct {
	Node  string `json:"n"`
	Class string `json:"c"`
	Exp   int64  `json:"e"`
}

func signJoin(key []byte, c joinClaims) string {
	p, _ := json.Marshal(c)
	pb := base64.RawURLEncoding.EncodeToString(p)
	m := hmac.New(sha256.New, key)
	m.Write([]byte(pb))
	return pb + "." + base64.RawURLEncoding.EncodeToString(m.Sum(nil))
}

func verifyJoin(key []byte, tok string) (joinClaims, error) {
	var c joinClaims
	pb, sigB64, found := strings.Cut(tok, ".")
	if !found {
		return c, errors.New("malformed join token")
	}
	m := hmac.New(sha256.New, key)
	m.Write([]byte(pb))
	sig, err := base64.RawURLEncoding.DecodeString(sigB64)
	if err != nil || !hmac.Equal(sig, m.Sum(nil)) {
		return c, errors.New("signature does not verify against the enrollment key")
	}
	p, err := base64.RawURLEncoding.DecodeString(pb)
	if err != nil {
		return c, errors.New("malformed join token")
	}
	if err := json.Unmarshal(p, &c); err != nil {
		return c, errors.New("malformed join token")
	}
	if c.Node == "" || strings.ContainsAny(c.Node, " \t/") {
		return c, errors.New("invalid node name")
	}
	if c.Class != "egress" && c.Class != "consumer" {
		return c, errors.New("invalid class")
	}
	if time.Now().Unix() > c.Exp {
		return c, fmt.Errorf("join token expired %s ago",
			time.Since(time.Unix(c.Exp, 0)).Truncate(time.Minute))
	}
	return c, nil
}

// ---------------------------------------------------------------- helpers

func randToken() string {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

func hashToken(t string) string {
	s := sha256.Sum256([]byte(t))
	return hex.EncodeToString(s[:])
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

// allocIP returns the lowest free address in the CIDR, skipping the hub.
func (h *Hub) allocIP() (string, error) {
	rows, err := h.db.Query(`SELECT ip FROM peers WHERE revoked_at IS NULL`)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	used := map[string]bool{h.hubIP.String(): true}
	for rows.Next() {
		var ip string
		if err := rows.Scan(&ip); err != nil {
			return "", err
		}
		used[ip] = true
	}

	ip := make(net.IP, len(h.cidr.IP.To4()))
	copy(ip, h.cidr.IP.To4())
	for h.cidr.Contains(ip) {
		// advance one address
		for i := len(ip) - 1; i >= 0; i-- {
			ip[i]++
			if ip[i] != 0 {
				break
			}
		}
		if ip[3] == 0 || ip[3] == 255 {
			continue
		}
		if !used[ip.String()] {
			return ip.String(), nil
		}
	}
	return "", errors.New("tunnel address space exhausted")
}

// syncPeer installs or replaces a peer on the live device. ReplacePeers is
// false so other tunnels are untouched — no restart, no dropped sessions.
func (h *Hub) syncPeer(pubkey, psk, ip string, remove bool) error {
	pk, err := wgtypes.ParseKey(pubkey)
	if err != nil {
		return fmt.Errorf("bad public key: %w", err)
	}
	cfg := wgtypes.PeerConfig{PublicKey: pk, Remove: remove}
	if !remove {
		k, err := wgtypes.ParseKey(psk)
		if err != nil {
			return err
		}
		_, allowed, err := net.ParseCIDR(ip + "/32")
		if err != nil {
			return err
		}
		ka := keepalive
		cfg.PresharedKey = &k
		cfg.AllowedIPs = []net.IPNet{*allowed}
		cfg.ReplaceAllowedIPs = true
		cfg.PersistentKeepaliveInterval = &ka
	}
	return h.wg.ConfigureDevice(h.device, wgtypes.Config{
		ReplacePeers: false,
		Peers:        []wgtypes.PeerConfig{cfg},
	})
}

// reconcile re-pushes every non-revoked peer. Called at startup so the device
// state is rebuilt from the database after a hub reboot or wg0 recreation.
func (h *Hub) reconcile() error {
	rows, err := h.db.Query(`SELECT pubkey, psk, ip FROM peers WHERE revoked_at IS NULL`)
	if err != nil {
		return err
	}
	defer rows.Close()
	n := 0
	for rows.Next() {
		var pub, psk, ip string
		if err := rows.Scan(&pub, &psk, &ip); err != nil {
			return err
		}
		if err := h.syncPeer(pub, psk, ip, false); err != nil {
			log.Printf("reconcile %s: %v", ip, err)
			continue
		}
		n++
	}
	log.Printf("reconciled %d peers onto %s", n, h.device)
	return nil
}

// ---------------------------------------------------------------- auth

func (h *Hub) fromTunnel(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && h.cidr.Contains(ip)
}

// authPeer resolves the bearer token to a live peer, and requires the request
// to originate from that peer's own tunnel address.
func (h *Hub) authPeer(r *http.Request) (clientID, ip string, ok bool) {
	if !h.fromTunnel(r) {
		return "", "", false
	}
	tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if tok == "" {
		return "", "", false
	}
	row := h.db.QueryRow(
		`SELECT client_id, ip FROM peers WHERE auth_hash = ? AND revoked_at IS NULL`,
		hashToken(tok))
	if err := row.Scan(&clientID, &ip); err != nil {
		return "", "", false
	}
	src, _, _ := net.SplitHostPort(r.RemoteAddr)
	if src != ip {
		log.Printf("token for %s presented from %s — rejected", ip, src)
		return "", "", false
	}
	return clientID, ip, true
}

// ---------------------------------------------------------------- handlers

type enrollReq struct {
	JoinToken string `json:"join_token"`
	Pubkey    string `json:"public_key"`
	Hostname  string `json:"hostname"`
}

type enrollResp struct {
	ClientID       string `json:"client_id"`
	Class          string `json:"class"`
	TunnelIP       string `json:"tunnel_ip"`
	TunnelCIDR     string `json:"tunnel_cidr"`
	PresharedKey   string `json:"preshared_key"`
	ServerPubkey   string `json:"server_public_key"`
	ServerEndpoint string `json:"server_endpoint"`
	ControlURL     string `json:"control_url"`
	AuthToken      string `json:"auth_token"`
	ProxyPort      int    `json:"proxy_port"`
	Keepalive      int    `json:"persistent_keepalive"`
	Reenrolled     bool   `json:"reenrolled"`
}

// handleEnroll is idempotent per node name. A node presenting a valid token
// for a name it already holds is re-keyed in place, keeping its tunnel address
// — so a wiped or rebuilt Mac rejoins as itself rather than leaking an address.
func (h *Hub) handleEnroll(w http.ResponseWriter, r *http.Request) {
	var req enrollReq
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&req); err != nil {
		fail(w, http.StatusBadRequest, "malformed body")
		return
	}
	claims, err := verifyJoin(h.joinKey, req.JoinToken)
	if err != nil {
		log.Printf("enroll rejected from %s: %v", r.RemoteAddr, err)
		fail(w, http.StatusUnauthorized, err.Error())
		return
	}
	if _, err := wgtypes.ParseKey(req.Pubkey); err != nil {
		fail(w, http.StatusBadRequest, "invalid public key")
		return
	}
	if req.Hostname == "" {
		req.Hostname = claims.Node
	}

	h.mu.Lock()
	defer h.mu.Unlock()

	var ip, oldPub string
	reenroll := h.db.QueryRow(
		`SELECT ip, pubkey FROM peers WHERE client_id = ? AND revoked_at IS NULL`,
		claims.Node).Scan(&ip, &oldPub) == nil

	if !reenroll {
		if ip, err = h.allocIP(); err != nil {
			fail(w, http.StatusInsufficientStorage, err.Error())
			return
		}
	}

	psk, err := wgtypes.GenerateKey()
	if err != nil {
		fail(w, http.StatusInternalServerError, "psk generation failed")
		return
	}
	authTok := randToken()

	if _, err = h.db.Exec(
		`INSERT INTO peers (client_id,class,hostname,pubkey,psk,ip,auth_hash,created_at)
		 VALUES (?,?,?,?,?,?,?,?)
		 ON CONFLICT(client_id) DO UPDATE SET
		   class=excluded.class, hostname=excluded.hostname, pubkey=excluded.pubkey,
		   psk=excluded.psk, auth_hash=excluded.auth_hash, revoked_at=NULL`,
		claims.Node, claims.Class, req.Hostname, req.Pubkey, psk.String(), ip,
		hashToken(authTok), time.Now().Unix()); err != nil {
		fail(w, http.StatusConflict, "that public key is already held by another node")
		return
	}
	if err = h.syncPeer(req.Pubkey, psk.String(), ip, false); err != nil {
		fail(w, http.StatusInternalServerError, "wg: "+err.Error())
		return
	}
	if reenroll && oldPub != req.Pubkey {
		_ = h.syncPeer(oldPub, "", ip, true)
	}

	verb := "enrolled"
	if reenroll {
		verb = "re-enrolled"
	}
	log.Printf("%s %s (%s) as %s from %s", verb, claims.Node, claims.Class, ip, r.RemoteAddr)

	writeJSON(w, http.StatusOK, enrollResp{
		ClientID: claims.Node, Class: claims.Class, TunnelIP: ip,
		TunnelCIDR: h.cidr.String(), PresharedKey: psk.String(),
		ServerPubkey: h.hubPub, ServerEndpoint: h.endpoint,
		ControlURL: "http://" + h.hubIP.String() + ":8080",
		AuthToken:  authTok, ProxyPort: h.proxyPt,
		Keepalive: int(keepalive.Seconds()), Reenrolled: reenroll,
	})
}

// handleRotate swaps a live peer to a freshly generated keypair, keeping its
// address and identity. Run it from cron on the client for key rotation.
func (h *Hub) handleRotate(w http.ResponseWriter, r *http.Request) {
	clientID, ip, ok := h.authPeer(r)
	if !ok {
		fail(w, http.StatusUnauthorized, "unauthorized")
		return
	}
	var req struct {
		Pubkey string `json:"public_key"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&req); err != nil {
		fail(w, http.StatusBadRequest, "malformed body")
		return
	}
	if _, err := wgtypes.ParseKey(req.Pubkey); err != nil {
		fail(w, http.StatusBadRequest, "invalid public key")
		return
	}

	h.mu.Lock()
	defer h.mu.Unlock()

	var oldPub, psk string
	if err := h.db.QueryRow(
		`SELECT pubkey, psk FROM peers WHERE client_id = ?`, clientID).
		Scan(&oldPub, &psk); err != nil {
		fail(w, http.StatusNotFound, "peer gone")
		return
	}
	if _, err := h.db.Exec(
		`UPDATE peers SET pubkey = ? WHERE client_id = ?`, req.Pubkey, clientID); err != nil {
		fail(w, http.StatusConflict, "key already in use")
		return
	}
	if err := h.syncPeer(req.Pubkey, psk, ip, false); err != nil {
		fail(w, http.StatusInternalServerError, "wg: "+err.Error())
		return
	}
	_ = h.syncPeer(oldPub, "", ip, true)

	log.Printf("rotated key for %s", clientID)
	writeJSON(w, http.StatusOK, map[string]string{"status": "rotated", "tunnel_ip": ip})
}

type peerView struct {
	ClientID  string `json:"client_id"`
	Class     string `json:"class"`
	Hostname  string `json:"hostname"`
	TunnelIP  string `json:"tunnel_ip"`
	ProxyURL  string `json:"proxy_url,omitempty"`
	Healthy   bool   `json:"healthy"`
	LastSeenS int64  `json:"last_handshake_secs_ago"`
}

// listPeers joins database identity with live handshake data from the kernel,
// so health is measured rather than self-reported.
func (h *Hub) listPeers(onlyEgress, onlyHealthy bool) ([]peerView, error) {
	dev, err := h.wg.Device(h.device)
	if err != nil {
		return nil, err
	}
	shake := map[string]time.Time{}
	for _, p := range dev.Peers {
		shake[p.PublicKey.String()] = p.LastHandshakeTime
	}

	q := `SELECT client_id, class, hostname, pubkey, ip FROM peers WHERE revoked_at IS NULL`
	if onlyEgress {
		q += ` AND class = 'egress'`
	}
	rows, err := h.db.Query(q)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []peerView{}
	for rows.Next() {
		var v peerView
		var pub string
		if err := rows.Scan(&v.ClientID, &v.Class, &v.Hostname, &pub, &v.TunnelIP); err != nil {
			return nil, err
		}
		if t, ok := shake[pub]; ok && !t.IsZero() {
			age := time.Since(t)
			v.Healthy = age < handshakeStale
			v.LastSeenS = int64(age.Seconds())
		} else {
			v.LastSeenS = -1
		}
		if v.Class == "egress" {
			v.ProxyURL = fmt.Sprintf("http://%s:%d", v.TunnelIP, h.proxyPt)
		}
		if onlyHealthy && !v.Healthy {
			continue
		}
		out = append(out, v)
	}
	return out, nil
}

func (h *Hub) handlePeers(w http.ResponseWriter, r *http.Request) {
	if _, _, ok := h.authPeer(r); !ok {
		fail(w, http.StatusUnauthorized, "unauthorized")
		return
	}
	egress := r.URL.Query().Get("class") == "egress"
	healthy := r.URL.Query().Get("healthy") == "true"
	peers, err := h.listPeers(egress, healthy)
	if err != nil {
		fail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"peers": peers, "count": len(peers)})
}

// ---------------------------------------------------------------- main

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	if len(os.Args) < 2 {
		fmt.Println("usage: hubd [serve|join-token|enroll-key|revoke] ...")
		os.Exit(2)
	}

	fs := flag.NewFlagSet(os.Args[1], flag.ExitOnError)
	dbPath := fs.String("db", "/var/lib/hubd/hubd.db", "sqlite path")
	device := fs.String("device", "wg0", "wireguard interface")
	cidrS := fs.String("cidr", "10.88.0.0/16", "tunnel address space")
	hubIPS := fs.String("hub-ip", "10.88.0.1", "hub tunnel address")
	endpoint := fs.String("endpoint", "", "public endpoint advertised to clients, host:port")
	listen := fs.String("listen", ":8080", "api listen address")
	proxyPt := fs.Int("proxy-port", 8888, "proxy port on egress peers")
	keyPath := fs.String("enroll-key", "/etc/hubd/enroll.key", "the single enrollment key")
	class := fs.String("class", "egress", "node class: egress|consumer")
	node := fs.String("node", "", "node name the join token is bound to")
	ttl := fs.Duration("ttl", defaultJoinTTL, "join token lifetime")
	rotateKey := fs.Bool("rotate", false, "with enroll-key: mint a new key, voiding all tokens")
	clientID := fs.String("client", "", "client id to revoke")
	_ = fs.Parse(os.Args[2:])

	db, err := sql.Open("sqlite", *dbPath+"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)")
	if err != nil {
		log.Fatal(err)
	}
	if _, err := db.Exec(schema); err != nil {
		log.Fatal(err)
	}

	_, cidr, err := net.ParseCIDR(*cidrS)
	if err != nil {
		log.Fatal(err)
	}

	switch os.Args[1] {
	case "join-token":
		key, err := loadOrCreateKey(*keyPath)
		if err != nil {
			log.Fatal(err)
		}
		if *node == "" {
			log.Fatal("-node is required, e.g. -node macmini-garage")
		}
		if *class != "egress" && *class != "consumer" {
			log.Fatal("-class must be egress or consumer")
		}
		exp := time.Now().Add(*ttl)
		fmt.Printf("node:    %s\nclass:   %s\nexpires: %s\ntoken:   %s\n",
			*node, *class, exp.UTC().Format(time.RFC3339),
			signJoin(key, joinClaims{Node: *node, Class: *class, Exp: exp.Unix()}))
		return

	case "enroll-key":
		if *rotateKey {
			if err := os.Remove(*keyPath); err != nil && !os.IsNotExist(err) {
				log.Fatal(err)
			}
			fmt.Println("previous key removed — every outstanding join token is now void")
			fmt.Println("enrolled peers are unaffected; they authenticate with their own tokens")
		}
		if _, err := loadOrCreateKey(*keyPath); err != nil {
			log.Fatal(err)
		}
		fmt.Printf("enrollment key: %s\n", *keyPath)
		return

	case "revoke":
		wg, err := wgctrl.New()
		if err != nil {
			log.Fatal(err)
		}
		var pub string
		if err := db.QueryRow(`SELECT pubkey FROM peers WHERE client_id = ?`, *clientID).
			Scan(&pub); err != nil {
			log.Fatal("no such client")
		}
		if _, err := db.Exec(`UPDATE peers SET revoked_at = ? WHERE client_id = ?`,
			time.Now().Unix(), *clientID); err != nil {
			log.Fatal(err)
		}
		pk, _ := wgtypes.ParseKey(pub)
		if err := wg.ConfigureDevice(*device, wgtypes.Config{
			Peers: []wgtypes.PeerConfig{{PublicKey: pk, Remove: true}},
		}); err != nil {
			log.Fatal(err)
		}
		fmt.Printf("revoked %s — tunnel dropped immediately\n", *clientID)
		return

	case "serve":
		if *endpoint == "" {
			log.Fatal("-endpoint is required, e.g. hub.example.com:51820")
		}
	default:
		log.Fatalf("unknown command %q", os.Args[1])
	}

	wg, err := wgctrl.New()
	if err != nil {
		log.Fatal("wgctrl: ", err)
	}
	dev, err := wg.Device(*device)
	if err != nil {
		log.Fatalf("device %s: %v (bring it up with wg-quick first)", *device, err)
	}

	h := &Hub{
		db: db, wg: wg, device: *device, endpoint: *endpoint,
		cidr: cidr, hubIP: net.ParseIP(*hubIPS),
		hubPub: dev.PublicKey.String(), proxyPt: *proxyPt,
	}
	if h.joinKey, err = loadOrCreateKey(*keyPath); err != nil {
		log.Fatal(err)
	}
	if err := h.reconcile(); err != nil {
		log.Fatal(err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/enroll", h.handleEnroll)
	mux.HandleFunc("POST /v1/rotate", h.handleRotate)
	mux.HandleFunc("GET /v1/peers", h.handlePeers)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "hub_pubkey": h.hubPub})
	})

	srv := &http.Server{
		Addr:              *listen,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      20 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	// Shut down cleanly on SIGTERM so a systemd restart or a deploy does not
	// cut an enrollment mid-flight, between the peer insert and the device
	// write. Tunnels are unaffected either way — they live in the kernel, not
	// in this process.
	errc := make(chan error, 1)
	go func() {
		log.Printf("hubd on %s | device %s | pubkey %s | endpoint %s",
			*listen, *device, h.hubPub, h.endpoint)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			errc <- err
		}
	}()

	sigc := make(chan os.Signal, 1)
	signal.Notify(sigc, syscall.SIGINT, syscall.SIGTERM)

	select {
	case err := <-errc:
		log.Fatal(err)
	case sig := <-sigc:
		log.Printf("received %s, draining", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("shutdown: %v", err)
		}
		if err := db.Close(); err != nil {
			log.Printf("db close: %v", err)
		}
		log.Print("stopped")
	}
}
