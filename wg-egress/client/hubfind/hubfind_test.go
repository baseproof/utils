package main

import (
	"context"
	"encoding/base64"
	"net"
	"testing"
	"time"
)

// Real DoH lookups against a name that has several A records.
func TestDoHCollectsAllRecords(t *testing.T) {
	for _, p := range dohProviders {
		ips, err := dohLookup(context.Background(), p, "example.com")
		if err != nil {
			t.Logf("%-10s unavailable: %v", p.name, err)
			continue
		}
		t.Logf("%-10s returned %d A records: %v", p.name, len(ips), ips)
		if len(ips) < 2 {
			t.Logf("  (only %d — multi-hub relies on all being collected)", len(ips))
		}
	}
}

func TestRankingPrefersProvenEndpoints(t *testing.T) {
	now := time.Now()
	a := &Agent{state: map[string]*EPState{}}
	a.stateFor("10.0.0.1:51820").LastOK = now.Add(-2 * time.Minute) // just worked
	a.stateFor("10.0.0.1:51820").LatencyMS = 40
	a.stateFor("10.0.0.2:51820").LastOK = now.Add(-30 * time.Hour) // worked long ago
	s3 := a.stateFor("10.0.0.3:51820")                             // failing now
	s3.ConsecFail = 3
	s3.CooldownUntil = now.Add(4 * time.Minute)

	got := a.rank([]Candidate{
		{"10.0.0.3:51820", "doh", 0}, {"10.0.0.2:51820", "seed", 0}, {"10.0.0.1:51820", "history", 0},
	})
	for i, c := range got {
		t.Logf("%d. %s (%s) score=%.1f", i+1, c.Endpoint, c.Source,
			a.stateFor(c.Endpoint).score(now))
	}
	if got[0].Endpoint != "10.0.0.1:51820" {
		t.Fatalf("expected the recently-proven endpoint first, got %s", got[0].Endpoint)
	}
	if got[2].Endpoint != "10.0.0.3:51820" {
		t.Fatalf("expected the cooling-down endpoint last, got %s", got[2].Endpoint)
	}
}

func TestCooldownGrowsAndResets(t *testing.T) {
	a := &Agent{state: map[string]*EPState{}}
	ep := "10.0.0.9:51820"
	for i := 1; i <= 6; i++ {
		a.recordFail(ep)
		t.Logf("failure %d -> cooldown %s", i,
			time.Until(a.stateFor(ep).CooldownUntil).Truncate(time.Second))
	}
	if cd := time.Until(a.stateFor(ep).CooldownUntil); cd > maxCooldown {
		t.Fatalf("cooldown %s exceeded cap %s", cd, maxCooldown)
	}
	a.recordOK(ep, 45*time.Millisecond)
	s := a.stateFor(ep)
	if s.ConsecFail != 0 || !s.CooldownUntil.IsZero() {
		t.Fatal("a success must clear the breaker")
	}
	t.Logf("after success: consecFail=%d cooldown cleared, latency=%.0fms", s.ConsecFail, s.LatencyMS)
}

func TestParseHubTXT(t *testing.T) {
	cases := []struct {
		in   string
		want string
		pri  int
		ok   bool
	}{
		{`"v=1 ep=34.28.11.5:51820 pri=10 pk=abc123"`, "34.28.11.5:51820", 10, true},
		{`v=1 ep=35.192.44.9:51820 pri=20 pk=def456`, "35.192.44.9:51820", 20, true},
		{`v=1 ep=10.0.0.1:51820 pri=5 region=us-east1 future=x`, "10.0.0.1:51820", 5, true},
		{`v=1 ep=34.28.11.5 pri=10`, "", 0, false},
		{`v=1 pri=10 pk=abc`, "", 0, false},
		{`some unrelated spf record`, "", 0, false},
	}
	for _, c := range cases {
		h, ok := parseHubTXT(c.in)
		if ok != c.ok || h.Endpoint != c.want || (c.ok && h.Priority != c.pri) {
			t.Fatalf("parse(%q) = %+v,%v want ep=%q pri=%d ok=%v", c.in, h, ok, c.want, c.pri, c.ok)
		}
		t.Logf("%-52s -> ep=%-20q pri=%d ok=%v", c.in, h.Endpoint, h.Priority, ok)
	}
}

func TestPriorityBreaksTiesAmongUntried(t *testing.T) {
	a := &Agent{state: map[string]*EPState{}}
	got := a.rank([]Candidate{
		{"10.0.0.9:51820", "txt", 30},
		{"10.0.0.7:51820", "txt", 10},
		{"10.0.0.8:51820", "txt", 20},
	})
	for i, c := range got {
		t.Logf("%d. %s pri=%d", i+1, c.Endpoint, c.Priority)
	}
	if got[0].Endpoint != "10.0.0.7:51820" {
		t.Fatalf("lowest pri should lead, got %s", got[0].Endpoint)
	}
}

// Parse the record that is actually published, fetched over real DoH.
func TestLiveBaseproofRecord(t *testing.T) {
	for _, p := range dohProviders {
		recs, err := dohTXT(context.Background(), p, "_hub.baseproof.net")
		if err != nil {
			t.Logf("%-10s: %v", p.name, err)
			continue
		}
		for _, r := range recs {
			h, ok := parseHubTXT(r)
			if !ok {
				t.Fatalf("%s: failed to parse %q", p.name, r)
			}
			t.Logf("%-10s ep=%s pri=%d pk=%s", p.name, h.Endpoint, h.Priority, h.Pubkey)
			// Assert shape, not a specific value — the key changes whenever the
			// hub VM is rebuilt, and a test that pins it fails on every rotation.
			if _, _, err := net.SplitHostPort(h.Endpoint); err != nil {
				t.Fatalf("endpoint %q is not host:port: %v", h.Endpoint, err)
			}
			raw, err := base64.StdEncoding.DecodeString(h.Pubkey)
			if err != nil || len(raw) != 32 {
				t.Fatalf("pk is not a 32-byte base64 wireguard key: %q (%v)", h.Pubkey, err)
			}
			if h.Priority <= 0 {
				t.Fatalf("priority missing or non-positive: %d", h.Priority)
			}
		}
	}
}
