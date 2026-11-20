export type JiraDeployment = "cloud" | "data_center";

export function jiraDeploymentForUrl(
    url: string,
    previous?: { url: string; deployment: JiraDeployment },
): JiraDeployment {
    const hostname = new URL(url).hostname.toLowerCase();
    if (hostname === "atlassian.net" || hostname.endsWith(".atlassian.net")) {
        return "cloud";
    }
    if (previous?.deployment === "cloud" && previous.url === url) {
        return "cloud";
    }
    return "data_center";
}
