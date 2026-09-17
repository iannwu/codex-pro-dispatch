# Compact claim host boundary investigation

Baseline: 07451e2. No live client or canonical state was accessed or mutated
by the patch or tests. Historical listener task output was read for diagnosis.

The recorded native calls failed in 8–14 ms with no returned error payload.
A subsequent diagnostic reported `used:false` and no native claim token.
Read-only activation imports succeeded. These establish failure before the
native fence, but do not identify the exact host failure or rejected guard.

The generated claim previously let native exceptions escape the tool boundary.
The wrapper then reported every missing claim receipt as uncertain, losing the
distinction between a reported guard rejection and absent host output. In
particular, open binds a turn ID and serve-existing requires that same turn;
continuing in a later turn must reject, even in the same owner task.

The patch returns caught native errors as a structured negative receipt and
preserves a specific changed-turn explanation. The outer wrapper rejects
failed, absent, malformed and thrown host results without retry. A negative
receipt does not establish that the fence was unconsumed and never authorizes
another attempt. Identity checks and all fences remain unchanged.

Regression fixtures model the observed empty failed host result both before
and after execution, and a host that suppresses native exceptions. They also
cover thrown transport errors, malformed/null receipts and failed envelopes
containing a positive receipt. All new cases send zero requests. A native
REPL probe using only synthetic objects verified the negative receipt format.

Limitation: this fixes the diagnostic boundary, not a demonstrated host root
cause. The original empty failure cannot prove a turn mismatch. A successful
live recovery is not claimed. Preserve the incident until a diagnostic receipt
or host-side error identifies the rejected operation. Never replay a possibly
consumed claim to obtain diagnostics.
