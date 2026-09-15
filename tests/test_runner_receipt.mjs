import test from 'node:test';
import assert from 'node:assert/strict';
import {runnerReceipt} from '../skills/codex-pro-dispatch/scripts/parked-activation.mjs';

test('receipt excludes long answers and diagnostic bodies without mutating delivery',()=>{
  const secret='PRIVATE-SPEC-CONTENT'.repeat(10000);
  for(const state of ['published','acknowledged','blocked','pending']) {
    const result={ok:true,request_id:'request-1',observation:state,
      answer:secret,payload:{text:secret},error:secret,trace:[secret],
      restoration:{error:secret},no_resend:true};
    const before=JSON.stringify(result),receipt=JSON.stringify(runnerReceipt(result));
    assert(!receipt.includes('PRIVATE-SPEC-CONTENT'));
    assert(receipt.length<400);
    assert.equal(JSON.stringify(result),before);
    assert.equal(runnerReceipt(result).state,state);
  }
});
test('receipt rejects arbitrary state and malformed identifiers',()=>{
  const receipt=runnerReceipt({request_id:'private answer\n',state:'private answer',pending_helper_session:'secret'});
  assert.equal(receipt.request_id,null);
  assert.equal(receipt.state,'unknown');
  assert.equal(receipt.pending_helper_session,null);
  assert.doesNotThrow(()=>runnerReceipt(null));
});
