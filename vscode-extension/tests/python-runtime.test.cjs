const assert = require("node:assert/strict");
const { mkdtemp, mkdir, rm, writeFile } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { findReusableVenv, pythonExecutable } = require("../dist/python-runtime.js");

test("reuses the newest complete versioned Python environment", async () => {
    const storage = await mkdtemp(path.join(os.tmpdir(), "secure-jira-mcp-"));
    try {
        const oldVenv = path.join(storage, "venv-0.1.19");
        const newVenv = path.join(storage, "venv-0.1.21");
        await mkdir(path.dirname(pythonExecutable(oldVenv)), { recursive: true });
        await mkdir(path.dirname(pythonExecutable(newVenv)), { recursive: true });
        await writeFile(pythonExecutable(oldVenv), "");
        await writeFile(pythonExecutable(newVenv), "");
        await mkdir(path.join(storage, "venv-0.2.0"));

        assert.equal(await findReusableVenv(storage), newVenv);
    } finally {
        await rm(storage, { recursive: true, force: true });
    }
});

test("returns no reusable environment when none has a Python executable", async () => {
    const storage = await mkdtemp(path.join(os.tmpdir(), "secure-jira-mcp-"));
    try {
        await mkdir(path.join(storage, "venv-0.1.21"));
        assert.equal(await findReusableVenv(storage), undefined);
    } finally {
        await rm(storage, { recursive: true, force: true });
    }
});
