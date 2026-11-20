import { access, readdir } from "node:fs/promises";
import * as path from "node:path";

export function pythonExecutable(venv: string): string {
    return process.platform === "win32"
        ? path.join(venv, "Scripts", "python.exe")
        : path.join(venv, "bin", "python");
}

export async function findReusableVenv(storage: string): Promise<string | undefined> {
    const entries = await readdir(storage, { withFileTypes: true });
    const candidates = entries
        .filter((entry) => entry.isDirectory() && /^venv-\d+\.\d+\.\d+$/.test(entry.name))
        .sort((left, right) => right.name.localeCompare(left.name, undefined, { numeric: true }));
    for (const candidate of candidates) {
        const candidatePath = path.join(storage, candidate.name);
        if (await pathExists(pythonExecutable(candidatePath))) {
            return candidatePath;
        }
    }
}

async function pathExists(filePath: string): Promise<boolean> {
    try {
        await access(filePath);
        return true;
    } catch (error) {
        if (error instanceof Error && "code" in error && error.code === "ENOENT") {
            return false;
        }
        throw error;
    }
}
