// Model programs run inside QuickJS; only explicit tool bindings cross to Python.
const fs = require("node:fs");
const { getQuickJS, shouldInterruptAfterDeadline } = require("quickjs-emscripten");
function readLine() {
  const bytes = [], buf = Buffer.alloc(1);
  while (fs.readSync(0, buf, 0, 1, null)) {
    if (buf[0] === 10) break;
    bytes.push(buf[0]);
    if (bytes.length > 1024 * 1024) throw Error("Input limit exceeded");
  }
  if (!bytes.length) throw Error("Input closed");
  return JSON.parse(Buffer.from(bytes).toString("utf8"));
}
function send(value) { process.stdout.write(JSON.stringify(value) + "\n"); }
(async () => {
  const { code } = readLine();
  const qjs = await getQuickJS();
  const rt = qjs.newRuntime();
  rt.setMemoryLimit(32 * 1024 * 1024);
  rt.setMaxStackSize(256 * 1024);
  rt.setInterruptHandler(shouldInterruptAfterDeadline(Date.now() + 3000));
  const vm = rt.newContext();
  const logs = [];
  let calls = 0, outputBytes = 0;
  try {
    const binding = vm.newFunction("__dispatch", (name, args) => {
      if (++calls > 16) throw Error("PTC tool-call limit exceeded");
      send({ type: "tool", name: vm.getString(name), args: vm.dump(args) });
      const reply = readLine();
      return vm.newString(JSON.stringify(reply));
    });
    vm.setProp(vm.global, "__dispatch", binding);
    binding.dispose();
    const log = vm.newFunction("__log", value => {
      const text = vm.getString(value);
      outputBytes += Buffer.byteLength(text);
      if (outputBytes > 65536) throw Error("PTC output limit exceeded");
      logs.push(text);
    });
    vm.setProp(vm.global, "__log", log);
    log.dispose();
    const source = `
      const tools = Object.freeze(Object.fromEntries(
        ["inspect_instance", "estimate_pressure", "validate_program"].map(name =>
          [name, async (args = {}) => JSON.parse(__dispatch(name, args))])));
      const console = Object.freeze({log: (...args) => __log(JSON.stringify(args))});
      let __result, __error, __done = false;
      (async () => { ${code}\n })().then(
        value => { __result = JSON.stringify(value ?? null); __done = true; },
        error => { __error = String(error); __done = true; });
    `;
    const evaluated = vm.evalCode(source);
    if (evaluated.error) {
      const error = vm.dump(evaluated.error); evaluated.error.dispose();
      throw Error(JSON.stringify(error));
    }
    evaluated.value.dispose();
    const jobs = rt.executePendingJobs();
    if (jobs.error) {
      const error = vm.dump(jobs.error); jobs.error.dispose();
      throw Error(JSON.stringify(error));
    }
    const result = vm.evalCode("JSON.stringify({done:__done,value:__result,error:__error})");
    if (result.error) {
      const error = vm.dump(result.error); result.error.dispose();
      throw Error(JSON.stringify(error));
    }
    const outer = JSON.parse(vm.getString(result.value)); result.value.dispose();
    if (!outer.done) throw Error("Program has an unresolved promise");
    if (outer.error) throw Error(outer.error);
    if (Buffer.byteLength(outer.value) + outputBytes > 65536) throw Error("PTC output limit exceeded");
    send({ type: "done", value: JSON.parse(outer.value), logs });
  } finally { vm.dispose(); rt.dispose(); }
})().catch(error => { send({ type: "error", error: error.message }); process.exitCode = 1; });
