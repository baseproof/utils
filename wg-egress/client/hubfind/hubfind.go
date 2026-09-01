// hubfind — hub discovery and failover agent for a WireGuard egress node.
//
// Finds a reachable hub, proves it works by observing a real handshake, and
// keeps proving it. Every discovery source is treated as fallible, including
// DNS-over-HTTPS, which consumer routers routinely block.
//
//	go mod init hubfind
//	go get golang.zx2c4.com/wireguard/wgctrl
//	go build -o hubfind .
//	sudo ./hubfind -config /usr/local/etc/wg-egress/hubfind.json
//
// Runs as root: it reads and writes the WireGuard device.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"math"
	"math/rand"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.zx2c4.com/wireguard/wgctrl"
	"golang.zx2c4.com/wireguard/wgctrl/wgtypes"
)

// ------------------------------------------------------------------ tuning

const (
	handshakeFresh = 200 * time.Second // healthy if the last handshake is newer
	probeWait      = 8 * time.Second   // how long one candidate gets to prove itself
	healthyTick    = 20 * time.Second
	minBackoff     = 15 * time.Second
	maxBackoff     = 10 * time.Minute
	maxCooldown    = 15 * time.Minute
	dnsTimeout     = 6 * time.Second
)

type Config struct {
	Interface   string   `json:"interface"`      // wg0
	HubDomain   string   `json:"hub_domain"`     // hub.example.com
	HubTXT      string   `json:"hub_txt"`        // _hub.example.com, optional
	HubPort     int      `json:"hub_port"`       // 51820
	HubPubkey   string   `json:"hub_public_key"` // base64 wg key
	HubTunnelIP string   `json:"hub_tunnel_ip"`  // 10.88.0.1
	ControlPort int      `json:"control_port"`   // 8080
	Seeds       []string `json:"seeds"`          // "34.28.11.5:51820", last-resort
	StateDir    string   `json:"state_dir"`
}

// ------------------------------------------------------------------ state

// EPState is what the agent has learned about one endpoint. Persisted, so a
// rebooted node starts from experience rather than from zero.
type EPState struct {
	Endpoint      string    `json:"endpoint"`
	OK            int       `json:"ok"`
	Fail          int       `json:"fail"`
	ConsecFail    int       `json:"consec_fail"`
	LastOK        time.Time `json:"last_ok"`
	CooldownUntil time.Time `json:"cooldown_until"`
	LatencyMS     float64   `json:"latency_ms"` // EWMA of handshake time
}

// score ranks endpoints: recent success counts most, then speed, minus a
// penalty for consecutive failures. Higher is better.
func (s *EPState) score(now time.Time) float64 {
	var sc float64
	if !s.LastOK.IsZero() {
		sc += 100 / (1 + now.Sub(s.LastOK).Hours())
	}
	if s.LatencyMS > 0 {
		sc += 50 / (1 + s.LatencyMS/100)
	}
	return sc - 10*float64(s.ConsecFail)
}

type Agent struct {
	cfg       Config
	wg        *wgctrl.Client
	peer      wgtypes.Key
	state     map[string]*EPState
	statePath string
}

func (a *Agent) stateFor(ep string) *EPState {
	if s, ok := a.state[ep]; ok {
		return s
	}
	s := &EPState{Endpoint: ep}
	a.state[ep] = s
	return s
}

func (a *Agent) loadState() {
	b, err := os.ReadFile(a.statePath)
	if err != nil {
		return
	}
	var list []*EPState
	if json.Unmarshal(b, &list) != nil {
		return
	}
	for _, s := range list {
		a.state[s.Endpoint] = s
	}
	log.Printf("loaded history for %d endpoints", len(list))
}

func (a *Agent) saveState() {
	list := make([]*EPState, 0, len(a.state))
	for _, s := range a.state {
		list = append(list, s)
	}
	b, err := json.MarshalIndent(list, "", "  ")
	if err != nil {
		return
	}
	tmp := a.statePath + ".tmp"
	if os.WriteFile(tmp, b, 0o600) == nil {
		_ = os.Rename(tmp, a.statePath) // atomic, survives a crash mid-write
	}
}

// ------------------------------------------------------------------ discovery

type Candidate struct {
	Endpoint string
	Source   string
	Priority int // from TXT pri=, lower first; 0 when unspecified
}

type dohProvider struct {
	name string
	url  string
	pin  string // resolver IP, dialed directly so we never need DNS to get DNS
}

var dohProviders = []dohProvider{
	{"cloudflare", "https://cloudflare-dns.com/dns-query", "1.1.1.1"},
	{"google", "https://dns.google/resolve", "8.8.8.8"},
	{"adguard", "https://dns.adguard-dns.com/resolve", "94.140.14.14"},
}

// pinnedClient dials a fixed IP while keeping the real hostname for SNI and
// certificate validation — the Go equivalent of curl --resolve.
func pinnedClient(ip string) *http.Client {
	d := &net.Dialer{Timeout: dnsTimeout}
	return &http.Client{
		Timeout: dnsTimeout,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				_, port, err := net.SplitHostPort(addr)
				if err != nil {
					return nil, err
				}
				return d.DialContext(ctx, network, net.JoinHostPort(ip, port))
			},
		},
	}
}

// dohLookup returns every A record, not just the first — that is what lets one
// name carry several hubs.
func dohLookup(ctx context.Context, p dohProvider, name string) ([]string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET",
		fmt.Sprintf("%s?name=%s&type=A", p.url, name), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("accept", "application/dns-json")

	resp, err := pinnedClient(p.pin).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("%s: http %d", p.name, resp.StatusCode)
	}

	var body struct {
		Answer []struct {
			Type int    `json:"type"`
			Data string `json:"data"`
		} `json:"Answer"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	var out []string
	for _, ans := range body.Answer {
		if ans.Type == 1 && net.ParseIP(ans.Data) != nil { // type 1 = A, skips CNAMEs
			out = append(out, ans.Data)
		}
	}
	if len(out) == 0 {
		return nil, errors.New("no A records")
	}
	return out, nil
}

// hubTXT is one published hub: "v=1 ep=34.28.11.5:51820 pri=10 pk=<key>".
// Unknown fields are ignored so the format can grow without breaking old
// clients, and a record missing ep= is skipped rather than failing the lookup.
type hubTXT struct {
	Endpoint string
	Priority int
	Pubkey   string
}

func parseHubTXT(s string) (hubTXT, bool) {
	var h hubTXT
	s = strings.Trim(s, "\"")
	for _, field := range strings.Fields(s) {
		k, v, ok := strings.Cut(field, "=")
		if !ok {
			continue
		}
		switch k {
		case "ep":
			if _, _, err := net.SplitHostPort(v); err == nil {
				h.Endpoint = v
			}
		case "pri":
			if n, err := strconv.Atoi(v); err == nil {
				h.Priority = n
			}
		case "pk":
			h.Pubkey = v
		}
	}
	return h, h.Endpoint != ""
}

// dohTXT fetches TXT records (type 16) through a pinned DoH resolver.
func dohTXT(ctx context.Context, p dohProvider, name string) ([]string, error) {
	req, err := http.NewRequestWithContext(ctx, "GET",
		fmt.Sprintf("%s?name=%s&type=TXT", p.url, name), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("accept", "application/dns-json")
	resp, err := pinnedClient(p.pin).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("%s: http %d", p.name, resp.StatusCode)
	}
	var body struct {
		Answer []struct {
			Type int    `json:"type"`
			Data string `json:"data"`
		} `json:"Answer"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	var out []string
	for _, a := range body.Answer {
		if a.Type == 16 {
			out = append(out, a.Data)
		}
	}
	if len(out) == 0 {
		return nil, errors.New("no TXT records")
	}
	return out, nil
}

// discover merges every source. Sources that fail are logged and skipped; the
// agent only gives up when all of them are empty.
func (a *Agent) discover(ctx context.Context) []Candidate {
	seen := map[string]bool{}
	var cands []Candidate
	add2 := func(ep, src string, pri int) {
		if !seen[ep] {
			seen[ep] = true
			cands = append(cands, Candidate{ep, src, pri})
		}
	}
	add := func(ep, src string) { add2(ep, src, 0) }

	// TXT first: it carries the port and priority, so it produces better
	// candidates than an A record can. Failures here are not fatal.
	if a.cfg.HubTXT != "" {
		got := false
		for _, p := range dohProviders {
			recs, err := dohTXT(ctx, p, a.cfg.HubTXT)
			if err != nil {
				continue
			}
			for _, r := range recs {
				if h, ok := parseHubTXT(r); ok {
					add2(h.Endpoint, "txt/"+p.name, h.Priority)
					got = true
				}
			}
			if got {
				break // one provider agreeing is enough; they read the same zone
			}
		}
		if !got {
			txtCtx, cancel := context.WithTimeout(ctx, dnsTimeout)
			recs, err := net.DefaultResolver.LookupTXT(txtCtx, a.cfg.HubTXT)
			cancel()
			if err != nil {
				log.Printf("discovery: TXT %s unavailable (%v)", a.cfg.HubTXT, err)
			}
			for _, r := range recs {
				if h, ok := parseHubTXT(r); ok {
					add2(h.Endpoint, "txt/system", h.Priority)
				}
			}
		}
	}

	for _, p := range dohProviders {
		ips, err := dohLookup(ctx, p, a.cfg.HubDomain)
		if err != nil {
			log.Printf("discovery: doh/%s unavailable (%v)", p.name, err)
			continue
		}
		for _, ip := range ips {
			add(net.JoinHostPort(ip, fmt.Sprint(a.cfg.HubPort)), "doh/"+p.name)
		}
	}

	// The router's own resolver. Often the only one that works on a network
	// that blocks DoH, so it is a genuine fallback rather than a formality.
	sysCtx, cancel := context.WithTimeout(ctx, dnsTimeout)
	defer cancel()
	if ips, err := net.DefaultResolver.LookupHost(sysCtx, a.cfg.HubDomain); err == nil {
		for _, ip := range ips {
			if net.ParseIP(ip).To4() != nil {
				add(net.JoinHostPort(ip, fmt.Sprint(a.cfg.HubPort)), "system-dns")
			}
		}
	} else {
		log.Printf("discovery: system resolver failed (%v)", err)
	}

	for _, s := range a.cfg.Seeds {
		add(s, "seed")
	}

	// Anything that ever worked. Deliberately last in insertion order but not
	// last in ranking — if DNS is the broken thing, this is the way home.
	for ep, st := range a.state {
		if !st.LastOK.IsZero() {
			add(ep, "history")
		}
	}
	return cands
}

// ------------------------------------------------------------------ probing

func (a *Agent) lastHandshake() (time.Time, error) {
	dev, err := a.wg.Device(a.cfg.Interface)
	if err != nil {
		return time.Time{}, err
	}
	for _, p := range dev.Peers {
		if p.PublicKey == a.peer {
			return p.LastHandshakeTime, nil
		}
	}
	return time.Time{}, errors.New("hub peer not present on device")
}

func (a *Agent) setEndpoint(ep string) error {
	addr, err := net.ResolveUDPAddr("udp", ep)
	if err != nil {
		return err
	}
	return a.wg.ConfigureDevice(a.cfg.Interface, wgtypes.Config{
		Peers: []wgtypes.PeerConfig{{
			PublicKey:  a.peer,
			UpdateOnly: true, // never create a peer here; enrollment owns that
			Endpoint:   addr,
		}},
	})
}

// nudge queues a packet for the tunnel. WireGuard stays silent until it has
// something to send, so without this a dead session never retries.
func (a *Agent) nudge() {
	target := net.JoinHostPort(a.cfg.HubTunnelIP, fmt.Sprint(a.cfg.ControlPort))
	c, err := net.DialTimeout("tcp", target, 2*time.Second)
	if err == nil {
		_ = c.Close()
	}
}

// tryCandidate points the peer at one endpoint and waits for the kernel to
// report a fresh handshake. A handshake is unforgeable proof that this hub
// holds the right key and is actually up — far stronger than a ping or a
// TCP connect, which any middlebox can answer.
func (a *Agent) tryCandidate(ctx context.Context, ep string) (time.Duration, error) {
	before, err := a.lastHandshake()
	if err != nil {
		return 0, err
	}
	if err := a.setEndpoint(ep); err != nil {
		return 0, err
	}

	start := time.Now()
	deadline := time.After(probeWait)
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()

	go a.nudge()
	for {
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-deadline:
			return 0, fmt.Errorf("no handshake within %s", probeWait)
		case <-tick.C:
			now, err := a.lastHandshake()
			if err != nil {
				return 0, err
			}
			if now.After(before) {
				return time.Since(start), nil
			}
		}
	}
}

// internetUp distinguishes "this hub is down" from "this house is offline".
// Without it, a home outage would blackball every hub and the node would be
// crawling through cooldowns long after the link came back.
func internetUp() bool {
	for _, anchor := range []string{"1.1.1.1:443", "8.8.8.8:443", "9.9.9.9:443"} {
		if c, err := net.DialTimeout("tcp", anchor, 3*time.Second); err == nil {
			_ = c.Close()
			return true
		}
	}
	return false
}

// ------------------------------------------------------------------ trial

func (a *Agent) rank(cands []Candidate) []Candidate {
	now := time.Now()
	sort.SliceStable(cands, func(i, j int) bool {
		si, sj := a.stateFor(cands[i].Endpoint), a.stateFor(cands[j].Endpoint)
		ci, cj := si.CooldownUntil.After(now), sj.CooldownUntil.After(now)
		if ci != cj {
			return !ci // anything not cooling down goes first
		}
		if a, b := si.score(now), sj.score(now); a != b {
			return a > b
		}
		// Nothing learned yet about either: fall back to published priority.
		return cands[i].Priority < cands[j].Priority
	})
	return cands
}

func (a *Agent) recordOK(ep string, d time.Duration) {
	s := a.stateFor(ep)
	s.OK++
	s.ConsecFail = 0
	s.LastOK = time.Now()
	s.CooldownUntil = time.Time{}
	ms := float64(d.Milliseconds())
	if s.LatencyMS == 0 {
		s.LatencyMS = ms
	} else {
		s.LatencyMS = 0.7*s.LatencyMS + 0.3*ms // EWMA
	}
}

func (a *Agent) recordFail(ep string) {
	s := a.stateFor(ep)
	s.Fail++
	s.ConsecFail++
	back := time.Duration(math.Pow(2, float64(s.ConsecFail))) * 30 * time.Second
	if back > maxCooldown {
		back = maxCooldown
	}
	s.CooldownUntil = time.Now().Add(back)
}

// trial walks candidates best-first until one produces a handshake.
func (a *Agent) trial(ctx context.Context) bool {
	cands := a.rank(a.discover(ctx))
	if len(cands) == 0 {
		log.Print("trial: no candidates from any source")
		return false
	}

	online := internetUp()
	if !online {
		log.Print("trial: no internet — will retry without penalising any hub")
	}

	now := time.Now()
	for _, c := range cands {
		st := a.stateFor(c.Endpoint)
		cooling := st.CooldownUntil.After(now)
		if cooling && online {
			log.Printf("trial: skip %s (%s), cooling down %s",
				c.Endpoint, c.Source, time.Until(st.CooldownUntil).Truncate(time.Second))
			continue
		}
		d, err := a.tryCandidate(ctx, c.Endpoint)
		if err != nil {
			log.Printf("trial: %s (%s) failed: %v", c.Endpoint, c.Source, err)
			if online {
				a.recordFail(c.Endpoint)
			}
			continue
		}
		log.Printf("trial: %s (%s) handshook in %s — selected", c.Endpoint, c.Source, d.Truncate(time.Millisecond))
		a.recordOK(c.Endpoint, d)
		a.saveState()
		return true
	}

	a.saveState()
	return false
}

// ------------------------------------------------------------------ main

func (a *Agent) run(ctx context.Context) {
	backoff := minBackoff
	for {
		hs, err := a.lastHandshake()
		switch {
		case err != nil:
			log.Printf("device: %v", err)
		case time.Since(hs) < handshakeFresh:
			backoff = minBackoff
			select {
			case <-ctx.Done():
				return
			case <-time.After(healthyTick):
			}
			continue
		default:
			log.Printf("handshake stale (%s) — starting trial", time.Since(hs).Truncate(time.Second))
		}

		if a.trial(ctx) {
			backoff = minBackoff
			continue
		}

		// Jitter so a fleet that lost the hub together does not return as one.
		wait := backoff + time.Duration(rand.Int63n(int64(backoff/2+1)))
		log.Printf("all candidates failed, retrying in %s", wait.Truncate(time.Second))
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		if backoff *= 2; backoff > maxBackoff {
			backoff = maxBackoff
		}
	}
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	cfgPath := flag.String("config", "/usr/local/etc/wg-egress/hubfind.json", "config file")
	once := flag.Bool("once", false, "run a single trial and exit")
	flag.Parse()

	b, err := os.ReadFile(*cfgPath)
	if err != nil {
		log.Fatal(err)
	}
	var cfg Config
	if err := json.Unmarshal(b, &cfg); err != nil {
		log.Fatal(err)
	}
	if cfg.StateDir == "" {
		cfg.StateDir = filepath.Dir(*cfgPath)
	}

	peer, err := wgtypes.ParseKey(cfg.HubPubkey)
	if err != nil {
		log.Fatal("hub_public_key: ", err)
	}
	wg, err := wgctrl.New()
	if err != nil {
		log.Fatal("wgctrl: ", err)
	}
	defer wg.Close()

	a := &Agent{
		cfg: cfg, wg: wg, peer: peer,
		state:     map[string]*EPState{},
		statePath: filepath.Join(cfg.StateDir, "hubfind-state.json"),
	}
	a.loadState()

	ctx := context.Background()
	if *once {
		if !a.trial(ctx) {
			os.Exit(1)
		}
		return
	}
	log.Printf("hubfind: interface %s, hub %s:%d", cfg.Interface, cfg.HubDomain, cfg.HubPort)
	a.run(ctx)
}
