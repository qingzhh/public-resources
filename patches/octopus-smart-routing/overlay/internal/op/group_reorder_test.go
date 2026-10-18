package op

import (
	"context"
	"path/filepath"
	"testing"

	dbpkg "github.com/bestruirui/octopus/internal/db"
	"github.com/bestruirui/octopus/internal/model"
)

func TestGroupReorderPersistsAndRejectsDuplicateIDs(t *testing.T) {
	if dbpkg.GetDB() != nil {
		_ = dbpkg.Close()
	}
	if err := dbpkg.InitDB("sqlite", filepath.Join(t.TempDir(), "group-reorder.db"), false); err != nil {
		t.Fatalf("init db: %v", err)
	}
	t.Cleanup(func() { _ = dbpkg.Close() })
	if err := InitCache(); err != nil {
		t.Fatalf("init cache: %v", err)
	}

	ctx := context.Background()
	groups := []*model.Group{
		{Name: "first", Mode: model.GroupModeFailover},
		{Name: "second", Mode: model.GroupModeFailover},
		{Name: "third", Mode: model.GroupModeFailover},
	}
	for _, group := range groups {
		if err := GroupCreate(group, ctx); err != nil {
			t.Fatalf("create %s: %v", group.Name, err)
		}
	}

	ids := []int{groups[2].ID, groups[0].ID, groups[1].ID}
	if err := GroupReorder(&model.GroupReorderRequest{IDs: ids}, ctx); err != nil {
		t.Fatalf("reorder groups: %v", err)
	}
	listed, err := GroupList(ctx)
	if err != nil {
		t.Fatalf("list groups: %v", err)
	}
	for index, want := range ids {
		if listed[index].ID != want || listed[index].SortOrder != index+1 {
			t.Fatalf("group[%d] = id %d order %d, want id %d order %d", index, listed[index].ID, listed[index].SortOrder, want, index+1)
		}
	}

	if err := GroupReorder(&model.GroupReorderRequest{IDs: []int{groups[0].ID, groups[0].ID}}, ctx); err == nil {
		t.Fatal("duplicate group ids must be rejected")
	}
}
