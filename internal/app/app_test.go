package app

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const testKey = "test-api-key-at-least-thirty-two-bytes"

func testStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}
func TestAPI(t *testing.T) {
	s := testStore(t)
	handler := Handler(s, testKey)
	for _, tc := range []struct {
		name, key, body string
		want            int
	}{
		{"missing key", "", `{"content":"hello"}`, 401},
		{"wrong key", "wrong", `{"content":"hello"}`, 401},
		{"blank", testKey, `{"content":"  "}`, 400},
		{"invalid", testKey, `broken`, 400},
		{"extra field", testKey, `{"content":"hello","other":1}`, 400},
		{"two objects", testKey, `{"content":"hello"} {}`, 400},
		{"too long", testKey, `{"content":"` + strings.Repeat("a", 4097) + `"}`, 400},
		{"too large", testKey, `{"content":"` + strings.Repeat("a", 40000) + `"}`, 400},
		{"valid", testKey, `{"content":"Xin chào Telegram"}`, 202},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest("POST", "/api/v1/messages", strings.NewReader(tc.body))
			r.Header.Set("X-API-Key", tc.key)
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, r)
			if w.Code != tc.want {
				t.Fatalf("got %d: %s", w.Code, w.Body.String())
			}
		})
	}
	var count int
	var content, created, status string
	if err := s.db.QueryRow("SELECT count(*),content,created_at,status FROM messages").Scan(&count, &content, &created, &status); err != nil {
		t.Fatal(err)
	}
	if count != 1 || content != "Xin chào Telegram" || status != "pending" {
		t.Fatalf("unexpected stored message: %d %s %s", count, content, status)
	}
	if _, err := time.Parse(time.RFC3339Nano, created); err != nil {
		t.Fatal(err)
	}
}

type fakeSender struct {
	fail     bool
	contents []string
}

func (f *fakeSender) Send(_ context.Context, content string) (int64, error) {
	f.contents = append(f.contents, content)
	if f.fail {
		return 0, errors.New("test failure")
	}
	return 42, nil
}
func TestDurableRetry(t *testing.T) {
	path := filepath.Join(t.TempDir(), "queue.db")
	s, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	now := timestamp()
	_, err = s.db.Exec("INSERT INTO messages(content,created_at,next_attempt_at) VALUES(?,?,?)", "persist me", now, now)
	if err != nil {
		t.Fatal(err)
	}
	s.Close()
	s, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	sender := &fakeSender{fail: true}
	if err = deliver(context.Background(), s, sender); err == nil {
		t.Fatal("expected failure")
	}
	var attempts int
	var status string
	if err = s.db.QueryRow("SELECT attempts,status FROM messages").Scan(&attempts, &status); err != nil {
		t.Fatal(err)
	}
	if attempts != 1 || status != "pending" {
		t.Fatalf("lost pending message: %d %s", attempts, status)
	}
	sender.fail = false
	if err = deliver(context.Background(), s, sender); err != nil {
		t.Fatal(err)
	}
	if len(sender.contents) != 1 {
		t.Fatal("retry happened before scheduled time")
	}
	if _, err = s.db.Exec("UPDATE messages SET next_attempt_at=?", time.Now().Add(-time.Minute).UTC().Format(time.RFC3339Nano)); err != nil {
		t.Fatal(err)
	}
	if err = deliver(context.Background(), s, sender); err != nil {
		t.Fatal(err)
	}
	var messageID int64
	var sentAt string
	if err = s.db.QueryRow("SELECT status,telegram_message_id,sent_at FROM messages").Scan(&status, &messageID, &sentAt); err != nil {
		t.Fatal(err)
	}
	if status != "sent" || messageID != 42 || sentAt == "" {
		t.Fatal("message not marked sent")
	}
	if err = deliver(context.Background(), s, sender); err != nil {
		t.Fatal(err)
	}
	if len(sender.contents) != 2 {
		t.Fatal("sent message delivered again")
	}
}
func TestTelegram(t *testing.T) {
	for _, ok := range []bool{true, false} {
		t.Run(map[bool]string{true: "success", false: "rejection"}[ok], func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "POST" {
					t.Error("expected POST")
				}
				if err := r.ParseForm(); err != nil {
					t.Error(err)
				}
				if r.Form.Get("chat_id") != "@test" || r.Form.Get("text") != "hello" {
					t.Error("incorrect payload")
				}
				if ok {
					_, _ = w.Write([]byte(`{"ok":true,"result":{"message_id":123}}`))
				} else {
					w.WriteHeader(429)
					_, _ = w.Write([]byte(`{"ok":false}`))
				}
			}))
			defer server.Close()
			telegram := NewTelegram("secret", "@test")
			telegram.endpoint = server.URL
			id, err := telegram.Send(context.Background(), "hello")
			if ok && (err != nil || id != 123) {
				t.Fatalf("%d %v", id, err)
			}
			if !ok && err == nil {
				t.Fatal("rejection ignored")
			}
		})
	}
}

func TestWorkerPipeline(t *testing.T) {
	s := testStore(t)
	telegramServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Error(err)
		}
		if r.Form.Get("text") != "pipeline content" {
			t.Error("content lost")
		}
		_, _ = w.Write([]byte(`{"ok":true,"result":{"message_id":321}}`))
	}))
	defer telegramServer.Close()
	sender := NewTelegram("test-token", "@test")
	sender.endpoint = telegramServer.URL
	r := httptest.NewRequest("POST", "/api/v1/messages", strings.NewReader(`{"content":"pipeline content"}`))
	r.Header.Set("X-API-Key", testKey)
	w := httptest.NewRecorder()
	Handler(s, testKey).ServeHTTP(w, r)
	if w.Code != 202 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { defer close(done); Work(ctx, s, sender) }()
	defer func() { cancel(); <-done }()
	deadline := time.After(5 * time.Second)
	poll := time.NewTicker(20 * time.Millisecond)
	defer poll.Stop()
	for {
		select {
		case <-deadline:
			t.Fatal("worker did not deliver")
		case <-poll.C:
			var status string
			var id int64
			if err := s.db.QueryRow("SELECT status,COALESCE(telegram_message_id,0) FROM messages").Scan(&status, &id); err != nil {
				t.Fatal(err)
			}
			if status == "sent" {
				if id != 321 {
					t.Fatal("wrong Telegram ID")
				}
				return
			}
		}
	}
}
