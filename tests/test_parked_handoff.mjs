import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import {tmpdir} from "node:os";
import {execFile} from "node:child_process";
import {fileURLToPath} from "node:url";

const root=fileURLToPath(new URL("../",import.meta.url));
const script=root+"skills/codex-pro-dispatch/scripts/parked-activation.mjs";
const helper=root+"bin/pro-dispatch";
const AF=Object.getPrototypeOf(async function(){}).constructor;
const P="01a09f71-aa77-7502-8c1c-0d2ea8264ae9",N="01a0ac4a-313b-7f33-9ca0-f76d994ef745";
const J=JSON.stringify,mcp=v=>({content:[{type:"text",text:J(v)}]});
const pause=()=>new Promise(r=>setTimeout(r,10));
async function until(check){for(let i=0;i<1000;i++){if(await check())return;await pause();}throw Error("Fixture deadline");}

test("native handoff requires old context, real target and returned serve cleanup",async t=>{
 const d=await fs.realpath(await fs.mkdtemp(tmpdir()+"/handoff-"));
 const previous=process.env.CODEX_PRO_DISPATCH_HOME;
 process.env.CODEX_PRO_DISPATCH_HOME=d+"/authority";
 const env={...process.env};
 let g={},meta={threadId:P,"x-codex-turn-metadata":{turn_id:"fixture"}};
 async function run(file,args){return await new Promise((resolve,reject)=>{
  const child=execFile(file,args,{env,timeout:30000,maxBuffer:8388608},(e,out,err)=>
   e?reject(Error(err||e.message)):resolve(out));child.stdin.end();
 });}
 const cli=async args=>JSON.parse(await run("python3",[helper,...args]));
 const activation=async args=>JSON.parse(await run(process.execPath,[script,...args]));
 let serving,cleanupReturned=false,releaseCleanup;
 const heldCleanup=new Promise(r=>{releaseCleanup=r;});
 let hold=true,targetExists=true,targetKind="codex",reads=0,switchContext=false;
 const tools={
  async mcp__node_repl__js(a){
   const lines=[];
   await new AF("globalThis","nodeRepl","console",a.code)(g,{requestMeta:meta}, {log:v=>lines.push(String(v))});
   if(hold&&a.code.includes("stopResidentAdmission(o)")){
    cleanupReturned=true;await heldCleanup;
   }
   return {content:[{type:"text",text:lines.join("\n")}]};
  },
  async exec_command(a){return {exit_code:0,output:await run("/bin/sh",["-c",a.cmd])};},
  async mcp__codex_app__read_thread(a){
   reads++;assert.equal(a.threadId,N);
   if(switchContext)meta={...meta,threadId:N};
   return targetExists?mcp({schemaVersion:1,thread:{id:N,kind:targetKind,hostId:"local"}}):
    {isError:true,content:[{type:"text",text:"Task not found"}]};
  },
  async mcp__codex_app__send_message_to_thread(){assert.fail("Handoff must never send");}
 };
 const execute=code=>new AF("tools","text",code)(tools,()=>{});
 t.after(async()=>{
  releaseCleanup();
  if(g.parkedResident){
   await activation(["resident-stop",g.parkedResident.directory]).catch(()=>{});
   await serving?.catch(()=>{});
   await g.parkedResident.socket.close("fixture_cleanup").catch(()=>{});
  }
  if(previous===undefined)delete process.env.CODEX_PRO_DISPATCH_HOME;
  else process.env.CODEX_PRO_DISPATCH_HOME=previous;
  await fs.rm(d,{recursive:true,force:true});
 });
 await cli(["worker","set","--conversation-id","worker-a","--confirm-worker","--native-controls-confirmed"]);
 await run("python3",["-c",`import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,${J(root+"src")})
from codex_pro_dispatch import core,resident
p=core.default_paths()
e=Path(${J(d+"/proof.json")})
e.write_text(json.dumps(dict(kind='legacy_quiescence',config_dir=str(p.config_dir),state_dir=str(p.state_dir),implementation='fixture',observations='isolated fixture',authorization='unit test',physical_quiescence=True)))
e.chmod(0o600)
h=hashlib.sha256(e.read_bytes()).hexdigest()
core.activate_worker_pool([dict(slot='slot-a',conversation_id='worker-a',label='A',model_confirmation='user-confirmed-worker',configured_at='fixture')],expected_legacy_sha256=hashlib.sha256(p.worker_file.read_bytes()).hexdigest(),evidence_file=e,evidence_sha256=h,paths=p)
resident.control('enroll',dict(generation=0,owner='fixture',parent=${J(P)},evidence_file=str(e),evidence_sha256=h),p)
`]);
 const packet=await activation(["resident-pool-packet",P,P,'["worker-a"]',d]);
 await execute(packet.calls.open);
 const dir=g.parkedResident.directory;
 const barrier=dir+"/resident-joined.json";
 const handoff=await activation(["resident-handoff-packet",N]);
 serving=execute(packet.calls.serve);serving.catch(()=>{});
 await until(async()=> (await fs.readdir(dir)).some(n=>n.startsWith("waiting-")));
 await activation(["resident-stop",dir]);
 await until(()=>cleanupReturned);
 await fs.access(dir+"/transport-audit.json");
 await assert.rejects(fs.access(barrier));
 await assert.rejects(execute(handoff.calls.handoff),/has not joined/);
 assert.equal(reads,0);
 hold=false;releaseCleanup();await serving;
 const joined=JSON.parse(await fs.readFile(barrier,"utf8"));
 assert.equal(joined.parent,P);
 const ownerFile=d+"/authority/state/resident-owner.json";
 const before=await fs.readFile(ownerFile,"utf8");
 const credentials={...JSON.parse(before),new_parent:N,attested:true};
 await assert.rejects(cli(["resident","handoff",J(credentials)]),/native handoff packet/);
 meta={...meta,threadId:N};
 await assert.rejects(execute(handoff.calls.handoff),/identity mismatch/);
 assert.equal(reads,0);
 meta={...meta,threadId:P};targetExists=false;
 await assert.rejects(execute(handoff.calls.handoff),/Invalid native handoff result/);
 assert.equal(await fs.readFile(ownerFile,"utf8"),before);
 targetExists=true;targetKind="chatgpt";
 await assert.rejects(execute(handoff.calls.handoff),/does not exist locally/);
 assert.equal(await fs.readFile(ownerFile,"utf8"),before);
 targetKind="codex";
 switchContext=true;
 await assert.rejects(execute(handoff.calls.handoff),/identity mismatch/);
 assert.equal(await fs.readFile(ownerFile,"utf8"),before);
 switchContext=false;meta={...meta,threadId:P};
 await execute(handoff.calls.handoff);
 const after=JSON.parse(await fs.readFile(ownerFile,"utf8"));
 assert.equal(after.parent,N);assert.equal(after.generation,JSON.parse(before).generation+1);
 await assert.rejects(execute(handoff.calls.handoff),/Resident owner replaced/);
 // A fresh native task can use ordinary open/serve after the committed handoff.
 g={};meta={threadId:N,"x-codex-turn-metadata":{turn_id:"replacement"}};
 const replacement=await activation(["resident-pool-packet",N,N,'["worker-a"]',d]);
 await execute(replacement.calls.open);
 serving=execute(replacement.calls.serve);serving.catch(()=>{});
 await until(async()=> (await fs.readdir(g.parkedResident.directory)).some(n=>n.startsWith("waiting-")));
 await activation(["resident-stop",g.parkedResident.directory]);await serving;
 assert.equal(g.parkedResident.serveJoined.parent,N);
});
