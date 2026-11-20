const assert = require("node:assert/strict");
const test = require("node:test");
const {
    connectionServerId,
    selectedConfluenceCredentials,
    selectedJiraCredentials,
} = require("../dist/connection-selection.js");

test("selects only the requested Jira deployment", () => {
    const cloud = { deployment: "cloud", url: "https://cloud.example.com" };
    const dataCenter = { deployment: "data_center", url: "https://jira.example.com" };

    assert.deepEqual(selectedJiraCredentials([cloud, dataCenter], "jira:cloud"), [cloud]);
    assert.deepEqual(selectedJiraCredentials([cloud, dataCenter], "jira:data_center"), [dataCenter]);
});

test("selecting Confluence activates no Jira credential", () => {
    assert.deepEqual(
        selectedJiraCredentials([{ deployment: "cloud" }, { deployment: "data_center" }], "confluence"),
        [],
    );
    const credentials = { url: "https://confluence.example.com" };
    assert.equal(selectedConfluenceCredentials(credentials, "confluence"), credentials);
    assert.equal(selectedConfluenceCredentials(credentials, "jira:cloud"), undefined);
});

test("maps each active connection to its MCP server ID", () => {
    assert.equal(connectionServerId("publisher.extension", "jira:cloud"), "publisher.extension/Atlassian MCP for Copilot (Cloud)");
    assert.equal(connectionServerId("publisher.extension", "jira:data_center"), "publisher.extension/Atlassian MCP for Copilot (Data Center)");
    assert.equal(connectionServerId("publisher.extension", "confluence"), "publisher.extension/Atlassian MCP for Copilot (Confluence)");
});
