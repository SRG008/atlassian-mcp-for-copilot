const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..", "..");
const target = path.join(__dirname, "..", "server");

fs.mkdirSync(path.join(target, "src"), { recursive: true });
for (const name of ["pyproject.toml", "README.md"]) {
  fs.copyFileSync(path.join(root, name), path.join(target, name));
}
fs.cpSync(
  path.join(root, "src", "secure_jira_mcp"),
  path.join(target, "src", "secure_jira_mcp"),
  {
    recursive: true,
    filter: (source) => !source.split(path.sep).includes("__pycache__") && !source.endsWith(".pyc"),
  },
);
