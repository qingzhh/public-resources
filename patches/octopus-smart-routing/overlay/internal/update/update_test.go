package update

import "testing"

func TestGetUpdateSourceDisabledByDefault(t *testing.T) {
	t.Setenv(updateRepoEnv, "")
	_, enabled, err := getUpdateSource()
	if err != nil {
		t.Fatalf("get update source: %v", err)
	}
	if enabled {
		t.Fatal("managed custom build must not enable upstream updates by default")
	}
}

func TestGetUpdateSourceUsesConfiguredRepository(t *testing.T) {
	t.Setenv(updateRepoEnv, "example/octopus-custom")
	source, enabled, err := getUpdateSource()
	if err != nil {
		t.Fatalf("get update source: %v", err)
	}
	if !enabled {
		t.Fatal("configured update source should be enabled")
	}
	if source.RepositoryURL != "https://github.com/example/octopus-custom" {
		t.Fatalf("repository URL = %q", source.RepositoryURL)
	}
	if source.APIURL != "https://api.github.com/repos/example/octopus-custom/releases/latest" {
		t.Fatalf("api URL = %q", source.APIURL)
	}
}

func TestGetUpdateSourceRejectsOriginalNonGitHubOrInvalidPath(t *testing.T) {
	for _, value := range []string{"http://github.com/example/repo", "https://example.com/repo", "https://github.com/only-owner"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv(updateRepoEnv, value)
			if _, _, err := getUpdateSource(); err == nil {
				t.Fatalf("expected %q to be rejected", value)
			}
		})
	}
}
