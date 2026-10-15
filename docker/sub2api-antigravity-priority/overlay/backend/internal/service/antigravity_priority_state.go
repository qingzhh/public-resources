package service

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
)

func loadPriorityMissing(path string) (map[int64]bool, error) {
	result := map[int64]bool{}
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return result, nil
	}
	if err != nil {
		return nil, errors.New("cannot read priority host state")
	}
	var values map[string]bool
	if json.Unmarshal(raw, &values) != nil || values == nil {
		return nil, errors.New("invalid priority host state")
	}
	for key, value := range values {
		id, err := strconv.ParseInt(key, 10, 64)
		if err != nil || id <= 0 || strconv.FormatInt(id, 10) != key {
			return nil, errors.New("invalid priority host state identity")
		}
		if value {
			result[id] = true
		}
	}
	return result, nil
}

func (h *antigravityPriorityHost) priorityIsMissing(account *Account) bool {
	if account.Priority != 0 {
		return false
	}
	h.stateMu.RLock()
	defer h.stateMu.RUnlock()
	return h.missing[account.ID]
}

// Called under stateMu. This host-only metadata preserves CPA's absent priority
// semantics without modifying any OAuth credentials or extending the DB schema.
func (h *antigravityPriorityHost) setPriorityMissing(id int64, missing bool) error {
	if h.missing[id] == missing {
		return nil
	}
	updated := make(map[int64]bool, len(h.missing)+1)
	for key, value := range h.missing {
		updated[key] = value
	}
	if missing {
		updated[id] = true
	} else {
		delete(updated, id)
	}
	if h.statePath != "" {
		raw, err := json.Marshal(updated)
		if err != nil {
			return err
		}
		file, err := os.CreateTemp(filepath.Dir(h.statePath), ".priority-host-state-*")
		if err != nil {
			return errors.New("cannot create priority host state")
		}
		temporary := file.Name()
		defer func() { _ = os.Remove(temporary) }()
		if _, err = file.Write(raw); err != nil {
			_ = file.Close()
			return errors.New("cannot write priority host state")
		}
		if err = file.Sync(); err != nil {
			_ = file.Close()
			return errors.New("cannot sync priority host state")
		}
		if err = file.Close(); err != nil {
			return errors.New("cannot close priority host state")
		}
		if err = os.Rename(temporary, h.statePath); err != nil {
			return errors.New("cannot replace priority host state")
		}
	}
	h.missing = updated
	return nil
}
