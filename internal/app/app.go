package app

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	_ "github.com/mattn/go-sqlite3"
)

type Config struct{ APIKey, BotToken, ChatID, Addr, DatabasePath string }

func ConfigFromEnv() (Config, error) {
	c := Config{os.Getenv("API_KEY"), os.Getenv("TELEGRAM_BOT_TOKEN"), os.Getenv("TELEGRAM_CHAT_ID"), os.Getenv("HTTP_ADDR"), os.Getenv("DATABASE_PATH")}
	if len(c.APIKey) < 32 {
		return c, errors.New("API_KEY must contain at least 32 bytes")
	}
	if strings.TrimSpace(c.BotToken) == "" || strings.TrimSpace(c.ChatID) == "" {
		return c, errors.New("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
	}
	if c.Addr == "" {
		c.Addr = ":8080"
	}
	if c.DatabasePath == "" {
		c.DatabasePath = "./data/messages.db"
	}
	return c, nil
}

type Store struct{ db *sql.DB }

func Open(path string) (*Store, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
 CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 content TEXT NOT NULL,
 created_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT NOT NULL,
 sent_at TEXT,
 telegram_message_id INTEGER
 ); CREATE INDEX IF NOT EXISTS messages_pending ON messages(status,next_attempt_at);`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db}, nil
}
func (s *Store) Close() error { return s.db.Close() }
func timestamp() string       { return time.Now().UTC().Format(time.RFC3339Nano) }
func respond(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func Handler(s *Store, key string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		if err := s.db.PingContext(r.Context()); err != nil {
			respond(w, 503, map[string]string{"status": "unavailable"})
			return
		}
		respond(w, 200, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /api/v1/messages", func(w http.ResponseWriter, r *http.Request) {
		expected, provided := sha256.Sum256([]byte(key)), sha256.Sum256([]byte(r.Header.Get("X-API-Key")))
		if key == "" || subtle.ConstantTimeCompare(expected[:], provided[:]) != 1 {
			respond(w, 401, map[string]string{"error": "invalid API key"})
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 32<<10)
		var body struct {
			Content string `json:"content"`
		}
		dec := json.NewDecoder(r.Body)
		dec.DisallowUnknownFields()
		if err := dec.Decode(&body); err != nil {
			respond(w, 400, map[string]string{"error": "expected JSON body with content"})
			return
		}
		if err := dec.Decode(new(any)); err != io.EOF {
			respond(w, 400, map[string]string{"error": "expected a single JSON object"})
			return
		}
		if strings.TrimSpace(body.Content) == "" || !utf8.ValidString(body.Content) || len([]rune(body.Content)) > 4096 {
			respond(w, 400, map[string]string{"error": "content must contain 1 to 4096 characters and cannot be blank"})
			return
		}
		now := timestamp()
		result, err := s.db.ExecContext(r.Context(), "INSERT INTO messages(content,created_at,next_attempt_at) VALUES(?,?,?)", body.Content, now, now)
		if err != nil {
			respond(w, 500, map[string]string{"error": "could not save message"})
			return
		}
		id, err := result.LastInsertId()
		if err != nil {
			respond(w, 500, map[string]string{"error": "could not read message ID"})
			return
		}
		respond(w, 202, map[string]any{"id": id, "created_at": now, "status": "pending"})
	})
	return mux
}

type Sender interface {
	Send(context.Context, string) (int64, error)
}
type Telegram struct {
	endpoint, chatID string
	client           *http.Client
}

func NewTelegram(token, chatID string) *Telegram {
	return &Telegram{"https://api.telegram.org/bot" + token + "/sendMessage", chatID, &http.Client{Timeout: 10 * time.Second}}
}
func (t *Telegram) Send(ctx context.Context, content string) (int64, error) {
	form := url.Values{"chat_id": {t.chatID}, "text": {content}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return 0, errors.New("cannot construct Telegram request")
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	res, err := t.client.Do(req)
	if err != nil {
		return 0, errors.New("Telegram transport failed")
	}
	defer res.Body.Close()
	var body struct {
		OK     bool `json:"ok"`
		Result struct {
			MessageID int64 `json:"message_id"`
		} `json:"result"`
	}
	if err = json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&body); err != nil {
		return 0, errors.New("invalid Telegram response")
	}
	if res.StatusCode != 200 || !body.OK {
		return 0, fmt.Errorf("Telegram rejected message (HTTP %d)", res.StatusCode)
	}
	return body.Result.MessageID, nil
}

// SQLite is the durable queue. A single worker retries pending messages after restarts.
func Work(ctx context.Context, s *Store, sender Sender) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := deliver(ctx, s, sender); err != nil && ctx.Err() == nil {
				slog.Warn("delivery attempt failed", "error", err)
			}
		}
	}
}
func deliver(ctx context.Context, s *Store, sender Sender) error {
	var id int64
	var content string
	var attempts int
	err := s.db.QueryRowContext(ctx, `SELECT id,content,attempts FROM messages WHERE status='pending' AND julianday(next_attempt_at)<=julianday(?) ORDER BY id LIMIT 1`, timestamp()).Scan(&id, &content, &attempts)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	messageID, sendErr := sender.Send(ctx, content)
	if sendErr == nil {
		_, err = s.db.ExecContext(ctx, `UPDATE messages SET status='sent',sent_at=?,telegram_message_id=?,attempts=attempts+1 WHERE id=?`, timestamp(), messageID, id)
		return err
	}
	delay := time.Second * time.Duration(1<<min(attempts+1, 9))
	_, err = s.db.ExecContext(ctx, `UPDATE messages SET attempts=attempts+1,next_attempt_at=? WHERE id=?`, time.Now().UTC().Add(delay).Format(time.RFC3339Nano), id)
	if err != nil {
		return err
	}
	return sendErr
}
