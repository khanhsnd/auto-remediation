package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/joho/godotenv"
	"github.com/khanhsnd/auto-remediation/internal/app"
)

func main() {
	if err := run(); err != nil {
		slog.Error("server stopped", "error", err)
		os.Exit(1)
	}
}

func run() error {
	if err := godotenv.Load(); err != nil && !os.IsNotExist(err) {
		return err
	}
	cfg, err := app.ConfigFromEnv()
	if err != nil {
		return err
	}
	store, err := app.Open(cfg.DatabasePath)
	if err != nil {
		return err
	}
	defer store.Close()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	workerDone := make(chan struct{})
	go func() { defer close(workerDone); app.Work(ctx, store, app.NewTelegram(cfg.BotToken, cfg.ChatID)) }()
	server := &http.Server{Addr: cfg.Addr, Handler: app.Handler(store, cfg.APIKey), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	serverErr := make(chan error, 1)
	go func() { slog.Info("listening", "address", cfg.Addr); serverErr <- server.ListenAndServe() }()
	select {
	case <-ctx.Done():
	case err = <-serverErr:
		stop()
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	shutdownErr := server.Shutdown(shutdownCtx)
	<-workerDone
	if err != nil && !errors.Is(err, http.ErrServerClosed) {
		return err
	}
	return shutdownErr
}
