export type ActiveConnectionKey = "jira:cloud" | "jira:data_center" | "confluence";
export type JiraDeployment = "cloud" | "data_center";

export function jiraConnectionKey(deployment: JiraDeployment): ActiveConnectionKey {
    return deployment === "cloud" ? "jira:cloud" : "jira:data_center";
}

export function selectedJiraCredentials<T extends { deployment: JiraDeployment }>(
    credentials: T[],
    selection: ActiveConnectionKey,
): T[] {
    if (selection === "confluence") {
        return [];
    }
    const deployment = selection === "jira:cloud" ? "cloud" : "data_center";
    const selected = credentials.find((credential) => credential.deployment === deployment);
    return selected ? [selected] : [];
}

export function selectedConfluenceCredentials<T>(
    credentials: T | undefined,
    selection: ActiveConnectionKey,
): T | undefined {
    return selection === "confluence" ? credentials : undefined;
}

export function connectionServerId(extensionId: string, selection: ActiveConnectionKey): string {
    const serviceLabel = selection === "confluence"
        ? "Atlassian MCP for Copilot (Confluence)"
        : `Atlassian MCP for Copilot (${selection === "jira:cloud" ? "Cloud" : "Data Center"})`;
    return `${extensionId.toLowerCase()}/${serviceLabel}`;
}

export function connectionLabel(selection: ActiveConnectionKey): string {
    if (selection === "confluence") {
        return "Confluence";
    }
    return selection === "jira:cloud" ? "Jira Cloud" : "Jira Data Center";
}
