package webdav

import (
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

func TestConnectionRejectsReadOnlyBackupDirectory(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "PROPFIND":
			w.Header().Set("Content-Type", "application/xml; charset=utf-8")
			w.WriteHeader(http.StatusMultiStatus)
			_, _ = w.Write([]byte(`<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:"><D:response><D:href>/backups/</D:href><D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype><D:getcontentlength>0</D:getcontentlength></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>`))
		case "PUT":
			http.Error(w, "read-only", http.StatusForbidden)
		default:
			t.Fatalf("unexpected WebDAV request: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()

	err := TestConnection(&Config{URL: server.URL, BackupPath: "/backups"})
	if err == nil {
		t.Fatal("TestConnection succeeded for a read-only backup directory")
	}
}

func TestConnectionWritesAndRemovesCheckFile(t *testing.T) {
	var mu sync.Mutex
	var methods []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "PROPFIND":
			writeWebDAVDirectory(w, "/backups/")
		case "PUT", "DELETE":
			mu.Lock()
			methods = append(methods, r.Method)
			mu.Unlock()
			if r.Method == "PUT" {
				w.WriteHeader(http.StatusCreated)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Fatalf("unexpected WebDAV request: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()

	if err := TestConnection(&Config{URL: server.URL, BackupPath: "/backups"}); err != nil {
		t.Fatalf("TestConnection returned error: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(methods) != 2 || methods[0] != "PUT" || methods[1] != "DELETE" {
		t.Fatalf("expected write-check PUT then DELETE, got %v", methods)
	}
}
