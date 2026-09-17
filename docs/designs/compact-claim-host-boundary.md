# Compact claim host boundary investigation

Baseline: 07451e2. No live client or canonical state was accessed or mutated
by the patch or tests. Historical listener task output was read for diagnosis.

The recorded native calls failed in 8–14 ms with no returned error payload.
A subsequent diagnostic reported `used:false` and no native claim token.
Read-only activation imports succeeded. Follow-up diagnosis confirmed the
recovery call runs in a later turn of the same canonical owner task.

The generated claim previously let native exceptions escape the tool boundary.
The wrapper then reported every missing claim receipt as uncertain, losing the
distinction between a reported guard rejection and absent host output. In
particular, open binds a turn ID and serve-existing formerly required that same
turn, rejecting the intended later-turn recovery in the same owner task.

The patch returns caught native errors as a structured negative receipt and
preserves guard explanations. The outer wrapper rejects
failed, absent, malformed and thrown host results without retry. A negative
receipt does not establish that the fence was unconsumed and never authorizes
another attempt. After the pristine native checks, synchronous one-time fence,
and durable canonical claim succeed, recovery binds the retained native object
to the current trusted turn of the same owner task. Parent, generation, owner,
descriptor and socket identity remain checked. The old frozen binding is not
mutated. Relay and serving calls require the newly bound turn. Failed claims
never rebind; lost results remain consumed.

Regression fixtures model the observed empty failed host result both before
and after execution, and a host that suppresses native exceptions. They also
cover thrown transport errors, malformed/null receipts and failed envelopes
containing a positive receipt. All new cases send zero requests. A native
REPL probe using only synthetic objects verified the negative receipt format.

Focused tests prove later-turn recovery reaches an admission waiter without
sending requests, foreign tasks and malformed turn identities are rejected,
and lost results cannot claim again from another turn. Live recovery is not
performed by this patch. Never replay a possibly consumed claim to obtain
diagnostics.
