# Task Git Stage Design

Each task owns an ordered journal of exact patches under hidden `refs/harn/patches/<task>/...`. Every main-worktree turn records only the diff from its pre-turn checkpoint. Parallel merges append their already-isolated patches. Rollback reverse-applies the journal in reverse order and never falls back to a broad baseline checkout. Runtime history is cleared only after every patch is reversed successfully.

