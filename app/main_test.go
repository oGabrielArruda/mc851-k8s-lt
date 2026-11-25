package main

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestFib(t *testing.T) {
	want := map[int]int{0: 0, 1: 1, 2: 1, 10: 55, 20: 6765}
	for n, w := range want {
		if got := fib(n); got != w {
			t.Errorf("fib(%d)=%d, quer %d", n, got, w)
		}
	}
}

func TestHandlers(t *testing.T) {
	var ready atomic.Bool
	ready.Store(true)
	srv := httptest.NewServer(newMux(10, "pod-teste", &ready))
	defer srv.Close()

	cases := []struct {
		path string
		code int
		body string
	}{
		{"/fib", 200, "fib(10)=55"},
		{"/fib?n=20", 200, "fib(20)=6765"},
		{"/fib?n=abc", 400, ""},
		{"/fib?n=-1", 400, ""},
		{"/fib?n=99", 400, ""},
		{"/healthz", 200, ""},
		{"/readyz", 200, ""},
		{"/metrics", 200, "fib_requests_total"},
	}
	for _, c := range cases {
		resp, err := http.Get(srv.URL + c.path)
		if err != nil {
			t.Fatal(err)
		}
		buf := new(bytes.Buffer)
		_, _ = buf.ReadFrom(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != c.code {
			t.Errorf("%s: status %d, quer %d", c.path, resp.StatusCode, c.code)
		}
		if !strings.Contains(buf.String(), c.body) {
			t.Errorf("%s: corpo %q não contém %q", c.path, buf.String(), c.body)
		}
	}

	ready.Store(false)
	resp, err := http.Get(srv.URL + "/readyz")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("/readyz durante shutdown: status %d, quer 503", resp.StatusCode)
	}
}

func BenchmarkFib30(b *testing.B) {
	for b.Loop() {
		fib(30)
	}
}

func BenchmarkFib32(b *testing.B) {
	for b.Loop() {
		fib(32)
	}
}
