const assert = require("node:assert/strict");
const test = require("node:test");
const { atlassianServiceForUrl } = require("../dist/atlassian-service.js");

test("recognizes Confluence URLs by product hostname or path", () => {
    assert.equal(atlassianServiceForUrl("https://confluence.example.com"), "confluence");
    assert.equal(atlassianServiceForUrl("https://acme.atlassian.net/wiki/spaces/ENG"), "confluence");
});

test("recognizes Jira URLs by product hostname or path", () => {
    assert.equal(atlassianServiceForUrl("https://jira.example.com"), "jira");
    assert.equal(atlassianServiceForUrl("https://tools.example.com/jira"), "jira");
    assert.equal(atlassianServiceForUrl("https://acme.atlassian.net/browse/ENG-123"), "jira");
});

test("leaves shared or unrecognized URLs for the user to disambiguate", () => {
    assert.equal(atlassianServiceForUrl("https://acme.atlassian.net"), undefined);
    assert.equal(atlassianServiceForUrl("https://tools.example.com"), undefined);
});
