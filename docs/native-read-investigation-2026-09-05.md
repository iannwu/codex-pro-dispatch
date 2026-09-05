# Native ChatGPT read investigation — 2026-09-05

## Finding

The current desktop `read_thread` ChatGPT path is a lossy history projection,
not the evidence adapter required by pro-dispatch v1.2. Changing the requested
character limit or accepting omitted truncation flags cannot make it a verified
collection transport. The same path also cannot guarantee exact outbound bytes.

No new message was sent, no assignment was prepared, and no receipt or installed
app was modified during this investigation. Runtime collection requirements remain
unchanged. This report is not a live acceptance receipt.

## Inspected implementation

- App version: `26.901.41600`, build `7982`.
- Bundle: `/Applications/ChatGPT.app/Contents/Resources/app.asar`.
- Entry: `/webview/assets/app-initial-86767c3d23e5.js`.
- Entry SHA-256: `fb72076ee44f6596f8dafa9a3effe37a3527a87db21fde8b4042233957b554bb`.
- Tool handler: `RXi` → `vJi` → ChatGPT fallback `p_i`.
- Message projection: `b_i`; synthetic turn construction: `x_i`.

Names and behavior below apply only to this inspected bundle. They do not
establish a stable public API or authorize an adapter on other builds.

| Observation | Implementation evidence | Consequence |
| --- | --- | --- |
| Text is changed before collection | `b_i` joins text parts with two newlines, calls `.trim()`, then `.slice(0,t)` | Exact source bytes cannot be recovered from the summary |
| Small messages omit truncation | `b_i` sets `textTruncated` only when `i.length > t`; `x_i` serializes an undefined field away | Omission describes no additional shortening at this particular slice, not end-to-end integrity |
| Turn completion is synthetic | `x_i` sets every new turn's status to `completed`, including user-only turns | Completed turn status cannot establish completed assistant generation |
| Assistant status is discarded | `x_i` exposes type, ID, text, and optional truncation; it does not carry source generation state | Polling the same assistant text does not restore authoritative finality |
| Outer integrity is absent | `p_i` returns a summary envelope; `cK` JSON-serializes it without collection-integrity provenance | No selected-result outer-integrity evidence is available |
| Read limits are finite | Tool validation permits `maxOutputCharsPerItem` from 0 to 20,000 | Raising the limit cannot make arbitrary long messages exact |
| Slicing uses UTF-16 units | `b_i` uses JavaScript string length/slice | A boundary can split a supplementary Unicode character |
| History may be cached | Read path `h_i` uses `query.getOrFetch`; send path requests refetch | Repeated reads are not proof of fresh server state |

The helper's `codex-desktop-native-collection/v1` entry in
`src/codex_pro_dispatch/collection.py` is an allowlisted validation contract. It
does not implement a host reader or add missing native fields. Producing those
fields in an agent-written JSON file would not establish their provenance.

## Verification performed

1. Read the already configured worker by stable ID, with one turn and
   `includeOutputs: true`, at limits of 2 and 20,000. Both returned the same
   message IDs, no assistant-level generation status, omitted unshortened
   truncation fields, and no outer-integrity field. Increasing the limit did not
   add a richer schema.
2. A read at limit 1 failed in the tool transport with a serialization error.
   The existing assistant message contains a supplementary Unicode character.
   Splitting its surrogate pair is a plausible cause, corroborated by the source
   experiment below; the complete transport failure path was not inspected.
3. Extracted only the pure `b_i` and `x_i` projection functions and exercised
   them in a separate Node process with synthetic messages and stubbed conversion
   helpers. Nine assertions passed: user-only and partial-assistant inputs are
   labeled completed; whitespace is trimmed; unshortened truncation is omitted;
   shortening sets truncation; assistant status is absent; and a one-unit slice
   produces a lone high surrogate. This did not execute the app or access chat
   data from Node.
4. Ran `python3 -m unittest discover -s tests -p 'test_collection.py' -v`:
   all eight existing collection tests passed, including rejection of inferred
   turn finality and unknown truncation.

No private message bodies or conversation IDs are retained in this report.

## Recommended solution

### A. Connectivity-only smoke test for a simple message

Add a separate, explicitly named `probe` workflow when the goal is to establish
that a small message can be sent to the configured worker and a matching reply
can be observed. This is a proposed feature, not an implemented command.

- Permit only a fixed ASCII diagnostic prompt with a fresh random nonce and an
  exact expected ASCII reply. Do not accept arbitrary assignments or Git writes.
- Use the configured stable worker ID and existing user-confirmed Pro selection;
  never infer the model from a title.
- Refuse during an unresolved production dispatch or unusual-activity cooldown.
- Share the durable lock/no-resend discipline: arm before one native send;
  ambiguous outcomes remain collect-only; never automatically resend.
- Read the exact worker with the maximum supported character budget, associate
  the observed user/assistant IDs in the same returned turn, reject visible
  truncation, and require exact equality to the nonce reply. For this narrow
  ASCII fixture, text projection is an explicitly accepted test limitation.
- Return `reply_observed` with `verification_level: connectivity_only` and
  `generation_finality_verified: false`. Never emit production `completed`,
  verified content delivery, or host-acceptance success.
- Restore the parent task and retain only minimal identity/hash/state receipts.
- Test duplicate prevention, stale replies, wrong-worker replies, ambiguous sends,
  cooldowns, and separation from production completion before a live send.

This would satisfy the original hello-style test while accurately stating what
was tested. It does not solve verified inline/chunked collection. The current
skill requires full capability preflight for every dispatch, so the probe needs
its own documented diagnostic contract before use.

### B. Native host adapter for verified dispatch

Full dispatch needs a supported host operation that bypasses the lossy summary
projection and returns one coherent evidence object. The desktop host must:

1. Resolve the requested worker and selected user/assistant message IDs from one
   current conversation snapshot, including the actual reply association.
2. Expose source assistant generation status and its authoritative provenance;
   preserve incomplete, stopped, and failed outcomes instead of synthesizing
   completed turns.
3. Return exact source text without trimming or joining transformed text parts.
   Reject unsupported content forms explicitly. Provide the same exact-byte read
   guarantee for the submitted user message.
4. Expose message and selected-result outer truncation explicitly, with a
   versioned contract that covers the complete tool serialization path. Use safe
   Unicode boundaries when shortening is unavoidable.
5. Include observation time and enough source identity to detect stale reads,
   wrong branches, and incompatible message changes.

Then implement and allowlist the actual adapter in this repository, and run the
full native acceptance matrix against the inspected host build. An agent-side
mapping of synthetic `turn.status` to message finality is not that adapter.

The official [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server#item-lifecycle-events)
defines `item/completed` as the authoritative final item event for Codex work.
That documents the required kind of signal; it does not establish that the
desktop ChatGPT fallback exposes it. This investigation found no callable
ChatGPT collection surface in the current task with the required guarantees.

## Changes that do not resolve this issue

- Setting `includeOutputs: true`, increasing read size, or polling an unchanged
  answer cannot recreate discarded metadata or original bytes.
- Defaulting all missing flags to false would hide unknown outer integrity.
- Restoring the v1.1 marker-only completion path would discard the v1.2 guarantee.
- Chunking still needs trusted native evidence for every chunk.
- Artifact discovery is only a recovery route for an already authorized artifact
  assignment; it does not permit a new Git-writing hello test.
- Browser automation, app patching, clipboard access, and alternate model routes
  are outside this transport's supported contract.

The immediate engineering choice is a separately labeled diagnostic probe for
connectivity. Verified dispatch remains blocked on a richer native host adapter;
there is no proven local flag change that supplies its missing guarantees.
