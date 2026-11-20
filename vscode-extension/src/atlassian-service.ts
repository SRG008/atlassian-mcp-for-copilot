export type AtlassianService = "jira" | "confluence";

export function atlassianServiceForUrl(value: string): AtlassianService | undefined {
    const url = new URL(value);
    const hostLabels = url.hostname.toLowerCase().split(".");
    const pathSegments = url.pathname.toLowerCase().split("/").filter(Boolean);
    const labels = [...hostLabels, ...pathSegments];

    if (labels.includes("confluence") || labels.includes("wiki")) {
        return "confluence";
    }
    if (labels.includes("jira") || labels.includes("browse") || labels.includes("issues")) {
        return "jira";
    }
}
