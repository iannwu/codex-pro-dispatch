import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import {tmpdir} from "node:os";
import {spawnSync} from "node:child_process";
import {fileURLToPath} from "node:url";
const root=fileURLToPath(new URL("../",import.meta.url)),eq=assert.deepEqual;
const author=root+"skills/codex-pro-dispatch/scripts/parked-activation.mjs";
function fixture(t){
const d=fs.realpathSync(fs.mkdtempSync(tmpdir()+"/activation-unit-"));
t.after(()=>fs.rmSync(d,{recursive:true}));
const env={...process.env,CODEX_PRO_DISPATCH_HOME:d+"/authority"};
function run(file,args,input=""){
const r=spawnSync(file,args,{env,input,encoding:"utf8",timeout:30000,maxBuffer:8388608});
if(r.error) throw r.error;
return [r.status,JSON.parse(r.status?r.stderr:r.stdout)];
}
const cli=(a,input)=>run("python3",[root+"bin/pro-dispatch",...a],input);
const invoke=a=>run(process.execPath,[author,...a]);
eq(cli(["worker","set","--conversation-id","unit-pro","--confirm-pro","--native-controls-confirmed"])[0],0);
return {d,cli,invoke};
}
test("packet is complete; no native activation",t=>{
const f=fixture(t),[code,p]=f.invoke(["packet","broker","parent","unit-pro"]);
eq(code,0);eq(p.authorization,"required_separately");
eq(p.trusted.stateDir,f.d+"/authority/state");
const AF=Object.getPrototypeOf(async()=>{}).constructor;
for(const [name,body] of Object.entries(p.calls)){
assert.doesNotThrow(()=>new AF("tools","text",body));
const ms=name==="dispatch"?1000:60000;
assert(body.startsWith('// @exec: {"yield_time_ms":'+ms+'}'));
}
eq(p.trusted.idleMs,45000);
eq(p.trusted.activeJobMs,3600000);
eq(p.trusted.replyMs,3900000);
eq(p.trusted.leaseMs,7200000);
assert(p.calls.dispatch.includes("Native broker task/turn changed"));
eq(f.cli(["status"])[1].active_assignment,null);
eq(f.cli(["queue","status"])[1].requests,[]);
});
for(const busy of [true,false]) test(busy?"busy preserved":"wrong worker refused",t=>{
const f=fixture(t);
if(busy) eq(f.cli(["prepare","--assignment-id","unit-active","--parent-task-id",
"other-parent","--native-controls-confirmed"],"fixture")[0],0);
const before=f.cli(["status"])[1];
const [code,r]=f.invoke(["packet","broker","parent",busy?"unit-pro":"wrong"]);
assert.notEqual(code,0);assert.match(r.error,busy?/occupied/:/Production worker/);
eq(f.cli(["status"])[1],before);
});
test("readiness record, no native activation",t=>{
const f=fixture(t),d=f.d+"/session",sessionId="a".repeat(32);
fs.mkdirSync(d,{mode:448});
for(const [name,data] of [["session",{sessionId,leaseMs:20000,idleMs:5000,
replyMs:10000,expiresAt:Date.now()+20000}],
["ready-1",{sessionId,ordinal:1}]])
fs.writeFileSync(d+"/"+name+".json",JSON.stringify(data),{mode:384});
const [code,r]=f.invoke(["ready",d,"1"]);
eq([code,r.ready,r.ordinal],[0,true,1]);
assert.match(r.meaning,/not an admission guarantee/);
eq(f.cli(["status"])[1].active_assignment,null);
});
