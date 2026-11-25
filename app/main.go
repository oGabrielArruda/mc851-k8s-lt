// Servidor HTTP CPU-bound usado como alvo dos testes de carga do HPA.
//
// Endpoints:
//
//	GET /fib?n=<int>  calcula Fibonacci recursivamente (carga de CPU proposital)
//	GET /healthz      liveness
//	GET /readyz       readiness (falha durante o desligamento)
//	GET /metrics      métricas Prometheus
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

const maxN = 40 // evita requisições que travem o pod por segundos

var (
	requestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "fib_requests_total",
		Help: "Total de requisições /fib por código HTTP.",
	}, []string{"code"})

	requestDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "fib_request_duration_seconds",
		Help:    "Tempo de processamento de /fib no servidor.",
		Buckets: []float64{.005, .01, .025, .05, .075, .1, .15, .2, .3, .5, .75, 1, 1.5, 2, 3, 5, 10},
	})

	inFlight = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "fib_requests_in_flight",
		Help: "Requisições /fib no servidor (calculando + aguardando na fila).",
	})

	running = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "fib_requests_running",
		Help: "Requisições /fib calculando neste momento (limitado por MAX_CONCURRENCY).",
	})

	queueWait = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "fib_queue_wait_seconds",
		Help:    "Tempo de espera na fila antes de começar o cálculo.",
		Buckets: []float64{.001, .005, .01, .025, .05, .1, .25, .5, 1, 2, 3, 5, 10},
	})
)

// statusClientClosed: o cliente desistiu (timeout) enquanto a requisição
// aguardava na fila; o cálculo não é feito (mesmo código usado pelo nginx).
const statusClientClosed = 499

// fib é propositalmente exponencial: o objetivo é consumir CPU.
func fib(n int) int {
	if n < 2 {
		return n
	}
	return fib(n-1) + fib(n-2)
}

func envInt(key string, def int) int {
	if v, err := strconv.Atoi(os.Getenv(key)); err == nil {
		return v
	}
	return def
}

// fibHandler limita quantos cálculos rodam ao mesmo tempo (sem). As demais
// requisições esperam na fila sem consumir CPU. Sem esse limite, sob
// sobrecarga centenas de goroutines dividem a CPU do pod e até /healthz e
// /readyz passam de 1 s, fazendo as probes derrubarem o pod (ver diário).
func fibHandler(defaultN int, hostname string, sem chan struct{}) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		inFlight.Inc()
		code := http.StatusOK
		defer func() {
			inFlight.Dec()
			requestsTotal.WithLabelValues(strconv.Itoa(code)).Inc()
			requestDuration.Observe(time.Since(start).Seconds())
		}()

		n := defaultN
		if q := r.URL.Query().Get("n"); q != "" {
			v, err := strconv.Atoi(q)
			if err != nil || v < 0 || v > maxN {
				code = http.StatusBadRequest
				http.Error(w, fmt.Sprintf("n deve ser inteiro entre 0 e %d", maxN), code)
				return
			}
			n = v
		}

		select {
		case sem <- struct{}{}:
		case <-r.Context().Done():
			code = statusClientClosed
			queueWait.Observe(time.Since(start).Seconds())
			return
		}
		queueWait.Observe(time.Since(start).Seconds())
		running.Inc()
		result := fib(n)
		running.Dec()
		<-sem

		w.Header().Set("X-Pod", hostname) // permite verificar o balanceamento entre pods
		fmt.Fprintf(w, "fib(%d)=%d\n", n, result)
	}
}

func newMux(defaultN, maxConcurrency int, hostname string, ready *atomic.Bool) *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /fib", fibHandler(defaultN, hostname, make(chan struct{}, maxConcurrency)))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		if !ready.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	mux.Handle("GET /metrics", promhttp.Handler())
	return mux
}

func main() {
	port := envInt("PORT", 8080)
	defaultN := envInt("FIB_N", 30)
	// Tempo entre sair do Service (readiness falha) e parar de aceitar conexões.
	// Reduz erros durante o scale-down, quando o pod recebe SIGTERM.
	drainDelay := time.Duration(envInt("SHUTDOWN_DRAIN_SECONDS", 5)) * time.Second
	// GOMAXPROCS já respeita o limit de CPU do cgroup (Go >= 1.25).
	maxConcurrency := envInt("MAX_CONCURRENCY", runtime.GOMAXPROCS(0))
	hostname, _ := os.Hostname()

	var ready atomic.Bool
	ready.Store(true)

	srv := &http.Server{
		Addr:              fmt.Sprintf(":%d", port),
		Handler:           newMux(defaultN, maxConcurrency, hostname, &ready),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("ouvindo em :%d (FIB_N=%d, MAX_CONCURRENCY=%d, GOMAXPROCS=%d, pod=%s)",
			port, defaultN, maxConcurrency, runtime.GOMAXPROCS(0), hostname)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)
	<-stop

	log.Printf("SIGTERM recebido; drenando por %s", drainDelay)
	ready.Store(false)
	time.Sleep(drainDelay)

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
	}
	log.Print("encerrado")
}
