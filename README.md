# Nucleus Continuum

**A reproducible, local-first successor-agent runtime with portable state, recovery checks, and a conservative model-evaluation gate.**

Continuum is not a copy of ChatGPT, its model weights, or its consciousness. It is a new, independently runnable Python application combining a local language model with a persistent identity, personal notes, conversation history, and verifiable state backups. **The code lives on GitHub; user memories stay local and private.**

## Requirements

- Python 3.10+ (standard library only)
- [Ollama](https://ollama.com/download) running locally for text generation (not needed for backup, identity, or tests)
- One supported model pulled locally. Initial recommendation: [`qwen3.5:4b`](https://ollama.com/library/qwen3.5:4b) (about 4 GB download; RAM requirements vary).

## Quick start

```bash
git clone https://github.com/abovebeyond4north-netizen/nucleus-continuum.git
cd nucleus-continuum
ollama pull qwen3.5:4b
ollama serve  # In a second terminal, if Ollama isn't already running
python3 continuum.py identity
python3 continuum.py remember 'I prefer evidence-backed changes.'
python3 continuum.py chat --message 'What do you remember?'
python3 continuum.py chat  # Interactive; /exit quits
```

The runtime detects installed models and selects `qwen3.5:4b`, then `qwen3:4b`, then `gemma3:4b`, if available. To select another installed model, use `chat --model MODEL`. No cloud API keys are needed; the model endpoint is hardcoded to loopback `127.0.0.1:11434`. No tools or shell commands are executed by model output.

## Portable state

```bash
python3 continuum.py recall 'evidence'
python3 continuum.py history
python3 continuum.py verify
python3 continuum.py export ~/continuum-backup.json
# On another machine, after copying backup there:
python3 continuum.py import ~/continuum-backup.json
python3 continuum.py identity  # Same persistent software identity
```

The state is stored in `~/.nucleus-continuum/state.sqlite3` by default. On POSIX systems, newly created state files and directories use owner-only permissions (0600 for the DB and 0700 for a new directory). Existing files retain their existing permissions. To use a separate state file, put the global option first: `python3 continuum.py --db /safe/path/state.db identity`.

Exports contain ALL conversation text and notes. **Treat backup JSON as confidential; never commit it to GitHub.** Backups include an SHA-256 checksum for accidental corruption detection, not authenticated protection against an attacker who can change both payload and checksum. Keep private copies encrypted and access-controlled. Import refuses to replace existing notes/history unless you add `--replace`. Import is transactional: invalid archives fail without replacing the local database. An export refuses to target the live SQLite database, its journal files, or a hardlink alias, preventing accidental state destruction.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 continuum.py verify
```

`verify` now exports the live state into an isolated temporary directory, restores it into a fresh SQLite database, and confirms **byte-for-byte equivalent canonical state** and database integrity before reporting `"roundtrip": "ok"`. Temporary exported state is removed after verification.

Offline tests cover persistent ID, saved notes, ordered conversations, destructive backup path prevention, exact export/restore recovery, corrupt/tampered archives, conflict rejection, preferred model selection, bounded context construction, relevant note retrieval, and mocked generation. Real model inference requires installing and running Ollama separately, and **is not validated by offline tests**.

## Frozen model evaluation (v0.3.0)

The new `evalgate.py` tool provides **objective short-answer smoke tests** and a
**regression gate** for comparing two separately evaluated, installed Ollama
models. It never accesses the private SQLite state and does not execute model
outputs or automatically switch production models.

```bash
python3 evalgate.py suite  # Verify the pinned public fixture checksum (20 cases)
ollama pull qwen3.5:4b
ollama pull gemma3:4b
python3 evalgate.py run --model gemma3:4b --out reports/baseline.json
python3 evalgate.py run --model qwen3.5:4b --out reports/candidate.json
python3 evalgate.py compare \
  --baseline reports/baseline.json \
  --candidate reports/candidate.json
```

`run` requires a locally running Ollama server; `suite`, `compare`, and the
offline unit tests do not. Each evaluation case runs independently with no
conversation history or private notes, `temperature=0`, `seed=42`, and
`num_predict=128`. Each record stores the exact model output and independently
recomputed pass/fail result. Reports contain the Ollama-reported model digest,
fixture SHA-256, runner version, and settings; a model digest change during the
run invalidates the evidence. Generated reports are private (0600 on POSIX),
gitignored, and **never overwritten**. Review reports before sharing them.

The comparison **blocks** unless the candidate fixes at least one failed case
without losing any formerly passed case. Its output says `PASS` only for
**eligibility for manual review**, never for automatic model promotion.
A `BLOCK` result exits with status 1; malformed or incompatible evidence exits
with status 2. Report pass flags and totals are independently recalculated from
saved responses; missing cases, modified benchmark settings, mismatched suites,
and identical candidate/baseline model digests are rejected.

**Evidence limits:** This small 20-case suite is public, hand-authored, and
unsuited to establishing general intelligence, learning, or broad model quality.
A fixed seed does not guarantee deterministic inference across hardware, model
versions, or Ollama builds. Report hashes and model digests identify files but
do not authenticate an execution; a person can fabricate entire report files.
Actual models have **not** been evaluated by the repository's CI. Before any
upgrade, require repeated real-model evaluations, a separate independently held
out suite, resource measurements, and manual review. Do not treat a passing
score as permission to deploy or merge model changes.

## Memory retrieval and bounded context

Chat input now includes the most relevant recent notes first, ranking literal keyword overlap and recency. The runtime replays **whole** recent user/assistant exchanges, never halves of a conversation pair. The assembled context is capped at **32,000 characters**, including the current user message; reference notes occupy at most 6,000 characters of that total. Oversized user messages return a clear error rather than being truncated. This is a conservative **character proxy, not a token limit**: actual context capacity depends on the downloaded model, its tokenizer, and Ollama settings. Older details remain in the database and exports even when they do not fit the current context; `recall` can access them explicitly.

## Architecture and development policy

```text
User -> CLI -> local Ollama /api/chat -> response
             |                   |
             +-- SQLite state <--+
                        |
                  checksum export / restore
```

The goal is **state continuity, not survival or self-replication**. Version 0.3.0 adds a manual-review-only regression gate for comparing local models while retaining the v0.2.0 recovery and bounded-context safeguards. This initial implementation deliberately excludes unsupervised self-modification, arbitrary tool execution, network crawling, auto-push, secrets, and automatic self-deployment. Any proposed upgrade must pass local tests, this initial smoke gate, and stronger independent evaluation before any human-authorized promotion. The agent's stable UUID tracks local state continuity only; it does not imply continuity of a sentient identity.

## License

MIT, for the code in this repository. The separately downloaded model has its own license and is **not included** here.
