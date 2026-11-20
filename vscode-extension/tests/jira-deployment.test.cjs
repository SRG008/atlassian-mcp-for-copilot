const assert = require("node:assert/strict");
const test = require("node:test");
const { jiraDeploymentForUrl } = require("../dist/jira-deployment.js");

test("detects Jira Cloud from an Atlassian Cloud URL", () => {
    assert.equal(jiraDeploymentForUrl("https://acme.atlassian.net"), "cloud");
});

test("detects Jira Data Center for a self-hosted URL", () => {
    assert.equal(jiraDeploymentForUrl("https://jira.example.com/jira"), "data_center");
});

test("preserves a previously selected Cloud deployment for the same custom URL", () => {
    assert.equal(
        jiraDeploymentForUrl("https://jira.example.com", {
            url: "https://jira.example.com",
            deployment: "cloud",
        }),
        "cloud",
    );
});
