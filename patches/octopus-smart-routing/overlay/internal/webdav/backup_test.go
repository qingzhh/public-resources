package webdav

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/studio-b12/gowebdav"
)

func writeWebDAVDirectory(w http.ResponseWriter, hrefs ...string) {
	w.Header().Set("Content-Type", "application/xml; charset=utf-8")
	w.WriteHeader(http.StatusMultiStatus)
	_, _ = fmt.Fprint(w, `<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">`)
	for i, href := range hrefs {
		resourceType := `<D:resourcetype></D:resourcetype>`
		if i == 0 {
			resourceType = `<D:resourcetype><D:collection/></D:resourcetype>`
		}
		_, _ = fmt.Fprintf(w, `<D:response><D:href>%s</D:href><D:propstat><D:prop>%s<D:getcontentlength>1</D:getcontentlength></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>`, href, resourceType)
	}
	_, _ = fmt.Fprint(w, `</D:multistatus>`)
}

func TestEnsureBackupDirectoryCreatesAndRechecks(t *testing.T) {
	propfinds := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "PROPFIND":
			propfinds++
			if propfinds == 1 {
				http.NotFound(w, r)
				return
			}
			writeWebDAVDirectory(w, "/backups/")
		case "MKCOL":
			w.WriteHeader(http.StatusCreated)
		default:
			t.Fatalf("unexpected WebDAV request: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()

	if err := ensureBackupDirectory(gowebdav.NewClient(server.URL, "", ""), "/backups"); err != nil {
		t.Fatalf("ensureBackupDirectory returned error: %v", err)
	}
	if propfinds < 2 {
		t.Fatalf("expected a read after directory creation, got %d PROPFIND requests", propfinds)
	}
}

func TestEnsureBackupDirectoryAcceptsConcurrentCreate(t *testing.T) {
	propfinds := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "PROPFIND":
			propfinds++
			if propfinds == 1 {
				http.NotFound(w, r)
				return
			}
			writeWebDAVDirectory(w, "/backups/")
		case "MKCOL":
			http.Error(w, "already exists", http.StatusMethodNotAllowed)
		default:
			t.Fatalf("unexpected WebDAV request: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()

	if err := ensureBackupDirectory(gowebdav.NewClient(server.URL, "", ""), "/backups"); err != nil {
		t.Fatalf("ensureBackupDirectory returned error after a concurrent create: %v", err)
	}
}

func TestEnsureBackupDirectoryRejectsFalseMkdirSuccess(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "PROPFIND":
			http.NotFound(w, r)
		case "MKCOL":
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		default:
			t.Fatalf("unexpected WebDAV request: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()

	err := ensureBackupDirectory(gowebdav.NewClient(server.URL, "", ""), "/backups")
	if err == nil {
		t.Fatal("ensureBackupDirectory succeeded when MKCOL did not create the directory")
	}
	if !strings.Contains(err.Error(), "cannot access created backup directory") {
		t.Fatalf("unexpected directory validation error: %v", err)
	}
}

func TestEnforceRetentionReturnsDeleteError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "PROPFIND":
			writeWebDAVDirectory(
				w,
				"/backups/",
				"/backups/octopus-backup-20260101000000.json",
				"/backups/octopus-backup-20260102000000.json",
			)
		case "DELETE":
			http.Error(w, "denied", http.StatusForbidden)
		default:
			t.Fatalf("unexpected WebDAV request: %s %s", r.Method, r.URL.Path)
		}
	}))
	defer server.Close()

	err := enforceRetention(gowebdav.NewClient(server.URL, "", ""), "/backups", 1)
	if err == nil {
		t.Fatal("enforceRetention succeeded when deleting an expired backup failed")
	}
	if !strings.Contains(err.Error(), "failed to delete expired backup") {
		t.Fatalf("unexpected retention error: %v", err)
	}
}
