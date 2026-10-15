package service

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"time"
)

const antigravityPrioritySyncInterval = 5 * time.Second

type priorityAuthorityState struct {
	SchemaVersion int           `json:"schema_version"`
	NativeOffset  int           `json:"native_offset"`
	Priorities    map[int64]int `json:"priorities"`
}

func loadPriorityAuthority(path string) (map[int64]int, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, errors.New("cannot read priority authority state")
	}
	var state priorityAuthorityState
	if json.Unmarshal(raw, &state) != nil || state.SchemaVersion != 1 || state.NativeOffset != antigravityPriorityOffset || state.Priorities == nil {
		return nil, errors.New("invalid priority authority state")
	}
	for id, native := range state.Priorities {
		if _, err = translateAntigravityPriority(native); id <= 0 || err != nil {
			return nil, errors.New("invalid priority authority entry")
		}
	}
	return state.Priorities, nil
}

// Called under stateMu. Keep the most recent successful host value in memory
// even if persistence fails, and retry persistence before any reconciliation.
func (h *antigravityPriorityHost) rememberAppliedPriority(id int64, native int, missing bool) error {
	if h.priorities == nil {
		h.priorities = map[int64]int{}
	}
	if previous, ok := h.priorities[id]; !ok || previous != native {
		h.priorities[id] = native
		h.authorityDirty = true
	}
	if err := h.persistPriorityAuthority(); err != nil {
		return err
	}
	return h.setPriorityMissing(id, missing)
}

func (h *antigravityPriorityHost) persistPriorityAuthority() error {
	if !h.authorityDirty {
		return nil
	}
	if h.authorityPath != "" {
		raw, err := json.Marshal(priorityAuthorityState{SchemaVersion: 1, NativeOffset: antigravityPriorityOffset, Priorities: h.priorities})
		if err != nil {
			return err
		}
		file, err := os.CreateTemp(filepath.Dir(h.authorityPath), ".priority-authority-*")
		if err != nil {
			return errors.New("cannot create priority authority state")
		}
		temporary := file.Name()
		defer func() { _ = os.Remove(temporary) }()
		if _, err = file.Write(raw); err != nil {
			_ = file.Close()
			return errors.New("cannot write priority authority state")
		}
		if err = file.Sync(); err != nil {
			_ = file.Close()
			return errors.New("cannot sync priority authority state")
		}
		if err = file.Close(); err != nil {
			return errors.New("cannot close priority authority state")
		}
		if err = os.Rename(temporary, h.authorityPath); err != nil {
			return errors.New("cannot replace priority authority state")
		}
	}
	h.authorityDirty = false
	return nil
}

// Reconcile only effective priorities: neither quota predictions nor Status,
// Concurrency, Credentials, Extra or administrator scheduling flags are copied.
// Unknown accounts start from their current native value until the engine
// produces its first decision; deleted or no-longer-OAuth accounts are forgotten.
func (h *antigravityPriorityHost) reconcilePriorities(ctx context.Context, apply bool) (int, error) {
	h.stateMu.Lock()
	defer h.stateMu.Unlock()
	accounts, err := h.listAccounts(ctx)
	if err != nil {
		return 0, err
	}
	if h.priorities == nil {
		h.priorities = map[int64]int{}
		h.authorityDirty = true
	}
	eligible := make(map[int64]bool, len(accounts))
	for i := range accounts {
		account := &accounts[i]
		if !isPriorityAccount(account) {
			continue
		}
		eligible[account.ID] = true
		if _, known := h.priorities[account.ID]; !known {
			if _, err = translateAntigravityPriority(account.Priority); err != nil {
				return 0, err
			}
			h.priorities[account.ID] = account.Priority
			h.authorityDirty = true
		}
	}
	for id := range h.priorities {
		if !eligible[id] {
			delete(h.priorities, id)
			h.authorityDirty = true
		}
	}
	if err = h.persistPriorityAuthority(); err != nil {
		return 0, err
	}
	if !apply {
		return 0, nil
	}
	changed := 0
	var failures []error
	for i := range accounts {
		account := &accounts[i]
		if !eligible[account.ID] || account.Priority == h.priorities[account.ID] {
			continue
		}
		// Re-read before writing so a concurrent deletion/type change is respected.
		current, getErr := h.repo.GetByID(ctx, account.ID)
		if getErr != nil {
			failures = append(failures, errors.New("cannot read priority sync account"))
			continue
		}
		if !isPriorityAccount(current) || current.Priority == h.priorities[account.ID] {
			continue
		}
		native := h.priorities[account.ID]
		count, updateErr := h.repo.BulkUpdate(ctx, []int64{account.ID}, AccountBulkUpdate{Priority: &native})
		if updateErr != nil || count != 1 {
			failures = append(failures, errors.New("priority sync update failed"))
			continue
		}
		changed++
	}
	return changed, errors.Join(failures...)
}

func (s *AntigravityPriorityService) synchronizePriorityState(ctx context.Context) {
	taskCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	changed, err := s.host.reconcilePriorities(taskCtx, s.engine.AutomaticPrioritySyncEnabled(time.Now()))
	if err != nil && ctx.Err() == nil {
		slog.Warn("antigravity_priority_sync_failed")
	}
	if changed > 0 {
		slog.Info("antigravity_priority_sync_repaired", "accounts", changed)
	}
}

func (s *AntigravityPriorityService) startPrioritySync() {
	ctx, cancel := context.WithCancel(context.Background())
	s.syncCancel, s.syncDone = cancel, make(chan struct{})
	go func() {
		defer close(s.syncDone)
		ticker := time.NewTicker(antigravityPrioritySyncInterval)
		defer ticker.Stop()
		s.synchronizePriorityState(ctx)
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.synchronizePriorityState(ctx)
			}
		}
	}()
}
