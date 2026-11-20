import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { atlassianServiceForUrl } from "./atlassian-service";
import {
    connectionLabel,
    connectionServerId,
    jiraConnectionKey,
    selectedConfluenceCredentials,
    selectedJiraCredentials,
    type ActiveConnectionKey,
} from "./connection-selection";
import { jiraDeploymentForUrl } from "./jira-deployment";
import { findReusableVenv, pythonExecutable } from "./python-runtime";
import { access, cp, mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import * as path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import * as vscode from "vscode";

type Credentials = {
    url: string;
    deployment: "cloud" | "data_center";
    username?: string;
    apiToken?: string;
    apiTokenScoped?: boolean;
    pat?: string;
    revision: string;
};
type ConfluenceCredentials = Credentials;
const legacyCredentialsKey = "jiraConnection";
const jiraCredentialsKeys = {
    cloud: "jiraConnection:cloud",
    data_center: "jiraConnection:data_center",
} as const;
const confluenceCredentialsKey = "confluenceConnection";
const serverLabel = "Atlassian MCP for Copilot";
const isolatedArgs = ["-I", "-m", "secure_jira_mcp"];
const pythonCheck = "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)";

export function activate(context: vscode.ExtensionContext) {
    const changed = new vscode.EventEmitter<void>();
    const output = vscode.window.createOutputChannel("Atlassian MCP for Copilot");
    const version: string = context.extension.packageJSON.version;
    const activeConnectionKey = "activeConnection";
    const storage = context.globalStorageUri.fsPath;
    let venv = path.join(storage, "venv");
    let executable = pythonExecutable(venv);
    const installedKey = `installed:${version}`;
    let activeJiraCredentials: Credentials[] = [];
    let activeConfluenceCredentials: ConfluenceCredentials | undefined;
    let settingUp = false;

    context.subscriptions.push(changed, output);
    context.subscriptions.push(context.secrets.onDidChange(async ({ key }) => {
        if (key !== legacyCredentialsKey
            && key !== jiraCredentialsKeys.cloud
            && key !== jiraCredentialsKeys.data_center
            && key !== confluenceCredentialsKey) {
            return;
        }
        try {
            const selection = context.globalState.get<ActiveConnectionKey>(activeConnectionKey);
            if (!selection) {
                return;
            }
            const previousJiraSignature = credentialsSignature(activeJiraCredentials);
            const previousConfluenceRevision = activeConfluenceCredentials?.revision;
            activeJiraCredentials = selectedJiraCredentials(await getJiraCredentials(context), selection);
            activeConfluenceCredentials = selectedConfluenceCredentials(
                await getConfluenceCredentials(context),
                selection,
            );
            if (credentialsSignature(activeJiraCredentials) !== previousJiraSignature
                || activeConfluenceCredentials?.revision !== previousConfluenceRevision) {
                changed.fire();
            }
        } catch (error) {
            output.appendLine(`Could not refresh Atlassian credentials: ${String(error)}`);
        }
    }));
    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((event) => {
        if (event.affectsConfiguration("secureJiraMcp.allowWrites")
            || event.affectsConfiguration("secureJiraMcp.allowedNetworks")
            || event.affectsConfiguration("secureJiraMcp.trustHttpsProxy")) {
            changed.fire();
        }
    }));
    context.subscriptions.push(vscode.lm.registerMcpServerDefinitionProvider("secureJiraMcp", {
        onDidChangeMcpServerDefinitions: changed.event,
        provideMcpServerDefinitions() {
            const jiraCredentials = activeJiraCredentials;
            const confluenceCredentials = activeConfluenceCredentials;
            if ((jiraCredentials.length === 0 && !confluenceCredentials)
                || !context.globalState.get<boolean>(installedKey)) {
                return [];
            }
            const configuration = vscode.workspace.getConfiguration("secureJiraMcp");
            const allowWrites = configuration.get<boolean>("allowWrites", false);
            const allowedNetworks = configuration.get<string>("allowedNetworks", "").trim();
            const trustHttpsProxy = configuration.get<boolean>("trustHttpsProxy", false);
            const activeJira = jiraCredentials[0];
            const connections = activeJira
                ? [{ label: jiraServerLabel(activeJira.deployment), jira: activeJira, confluence: undefined }]
                : confluenceCredentials
                    ? [{ label: `${serverLabel} (Confluence)`, jira: undefined, confluence: confluenceCredentials }]
                    : [];
            return connections.map(({ label, jira, confluence }) => {
                const revisions = `${jira?.revision ?? ""}:${confluence?.revision ?? ""}`;
                const server = new vscode.McpStdioServerDefinition(
                    label,
                    executable,
                    [...isolatedArgs, "--transport", "stdio", "--no-env-file"],
                    {
                        ...(jira ? jiraEnvironment(jira, configuration) : {}),
                        ...(confluence ? confluenceEnvironment(confluence, configuration) : {}),
                        MCP_READ_ONLY: allowWrites ? "false" : "true",
                        MCP_AUDIT_LOG: path.join(storage, `audit-${label.replace(/[^A-Za-z0-9-]/g, "-")}.jsonl`),
                    },
                    `${version}:${revisions}:${allowWrites ? "write" : "read"}:${allowedNetworks}:${trustHttpsProxy}`,
                );
                server.cwd = context.globalStorageUri;
                return server;
            });
        },
    }));

    async function ensureInstalled(): Promise<boolean> {
        if (context.globalState.get<boolean>(installedKey)) {
            return true;
        }
        await mkdir(storage, { recursive: true });
        let environmentExists = await pathExists(executable);
        if (!environmentExists) {
            const reusableVenv = await findReusableVenv(storage);
            if (reusableVenv) {
                venv = reusableVenv;
                executable = pythonExecutable(venv);
                environmentExists = true;
            }
        }
        let python: { command: string; args: string[] } | undefined;
        if (!environmentExists) {
            python = await findPython(storage);
            if (!python) {
                return false;
            }
        }
        const dependencyManifest = await readFile(
            path.join(context.extensionPath, "server", "pyproject.toml"),
            "utf8",
        );
        const dependencyHash = createHash("sha256").update(dependencyManifest).digest("hex");
        const dependenciesChanged = context.globalState.get<string>("installedDependencyHash") !== dependencyHash;
        await vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: "Installing Atlassian MCP for Copilot in the background",
        }, async () => {
            if (!environmentExists) {
                if (!python) {
                    throw new Error("Python 3.11+ is required to install Atlassian MCP for Copilot.");
                }
                await run(python.command, [...python.args, "-I", "-m", "venv", venv], storage, output);
            }
            const staging = await mkdtemp(path.join(storage, "install-"));
            try {
                await cp(path.join(context.extensionPath, "server"), staging, { recursive: true });
                const installArgs = ["-I", "-m", "pip", "install", "--no-input"];
                if (environmentExists && !dependenciesChanged) {
                    installArgs.push("--no-deps", "--force-reinstall");
                }
                await run(executable, [...installArgs, staging], storage, output);
            } finally {
                await rm(staging, { recursive: true, force: true });
            }
            await run(executable, [...isolatedArgs, "--version"], storage, output);
            await context.globalState.update("installedDependencyHash", dependencyHash);
            await context.globalState.update(installedKey, true);
        });
        return true;
    }

    async function requestMcpStart(previousConnection?: ActiveConnectionKey): Promise<boolean> {
        try {
            // VS Code debounces collecting provider updates for 500 ms.
            await delay(750);
            if (previousConnection) {
                await vscode.commands.executeCommand(
                    "workbench.mcp.stopServer",
                    connectionServerId(context.extension.id, previousConnection),
                );
            }
            const selection = context.globalState.get<ActiveConnectionKey>(activeConnectionKey);
            if (!selection) {
                throw new Error("No active Atlassian MCP for Copilot connection is selected.");
            }
            await vscode.commands.executeCommand(
                "workbench.mcp.startServer",
                connectionServerId(context.extension.id, selection),
                { waitForLiveTools: true },
            );
            return true;
        } catch (error) {
            output.appendLine(`Could not request MCP startup: ${String(error)}`);
            const action = await vscode.window.showWarningMessage(
                "Atlassian credentials are saved, but VS Code could not start Atlassian MCP for Copilot automatically.",
                "Show Output",
            );
            if (action) {
                output.show();
            }
            return false;
        }
    }

    async function connect(automatic: boolean): Promise<void> {
        if (settingUp) {
            if (!automatic) {
                void vscode.window.showInformationMessage("Atlassian MCP for Copilot setup is already running.");
            }
            return;
        }
        settingUp = true;
        try {
            const previousConnections = await getJiraCredentials(context);
            const previous = previousConnections[0];
            const previousConfluence = await getConfluenceCredentials(context);
            const previousActiveConnection = context.globalState.get<ActiveConnectionKey>(activeConnectionKey);
            if (automatic && (previousConnections.length > 0 || previousConfluence)) {
                if (!await ensureInstalled()) {
                    return;
                }
                const hasSavedSelection = previousActiveConnection === "confluence"
                    ? Boolean(previousConfluence)
                    : previousConnections.some((credentials) =>
                        jiraConnectionKey(credentials.deployment) === previousActiveConnection);
                const selection = hasSavedSelection
                    ? previousActiveConnection
                    : previousConnections[0] ? jiraConnectionKey(previousConnections[0].deployment)
                        : previousConfluence ? "confluence" : undefined;
                if (!selection) {
                    return;
                }
                await context.globalState.update(activeConnectionKey, selection);
                activeJiraCredentials = selectedJiraCredentials(previousConnections, selection);
                activeConfluenceCredentials = selectedConfluenceCredentials(previousConfluence, selection);
                changed.fire();
                await requestMcpStart();
                return;
            }
            const url = await vscode.window.showInputBox({
                title: "Atlassian MCP for Copilot: Connect",
                prompt: "Enter the Jira or Confluence HTTPS URL. The service is inferred from its hostname or path.",
                placeHolder: "https://jira.example.com or https://confluence.example.com/wiki",
                validateInput: (value) => jiraUrl(value)
                    ? undefined : "Enter an HTTPS base URL without credentials, a query, or a fragment.",
            });
            if (url === undefined) {
                return;
            }
            const initialUrl = jiraUrl(url);
            if (!initialUrl) {
                throw new Error("Enter a valid HTTPS Jira or Confluence base URL.");
            }
            let service = atlassianServiceForUrl(initialUrl);
            if (!service) {
                service = previousConfluence?.url === initialUrl
                    ? "confluence"
                    : previousConnections.some(({ url: savedUrl }) => savedUrl === initialUrl) ? "jira" : undefined;
            }
            if (!service) {
                const serviceName = await vscode.window.showInputBox({
                    title: "Atlassian MCP for Copilot: Identify the service",
                    prompt: "This URL does not identify the Atlassian product. Type Jira or Confluence, or enter a product-specific URL.",
                    placeHolder: "Jira or Confluence",
                    validateInput: (value) => /^(jira|confluence)$/i.test(value.trim())
                        ? undefined : "Type Jira or Confluence.",
                });
                if (serviceName === undefined) {
                    return;
                }
                service = serviceName.trim().toLowerCase() as "jira" | "confluence";
            }
            const savedJira = previousConnections.find(({ url: savedUrl }) => savedUrl === initialUrl);
            if (service === "confluence") {
                if (previousConfluence?.url === initialUrl) {
                    await activateSavedConnection("confluence", previousConnections, previousConfluence);
                    return;
                }
                await connectConfluence(initialUrl, true);
                return;
            }
            if (savedJira) {
                await activateSavedConnection(
                    jiraConnectionKey(savedJira.deployment),
                    previousConnections,
                    previousConfluence,
                );
                return;
            }
            const normalizedUrl = initialUrl;
            const matchingPrevious = previousConnections.find(({ url }) => url === normalizedUrl);
            const deployment = jiraDeploymentForUrl(normalizedUrl, matchingPrevious ?? previous);
            let username: string | undefined;
            let apiTokenScoped = false;
            if (deployment === "cloud") {
                username = await vscode.window.showInputBox({
                    title: "Atlassian MCP for Copilot: Connect to Jira (2/3)",
                    prompt: "Enter the Atlassian account email associated with your API token.",
                    ...(matchingPrevious?.deployment === "cloud" && matchingPrevious.username
                        ? { value: matchingPrevious.username }
                        : previous?.deployment === "cloud" && previous.username
                            ? { value: previous.username }
                            : {}),
                    validateInput: (value) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
                        ? undefined : "Enter the Atlassian account email address.",
                });
                if (username === undefined) {
                    return;
                }
                const tokenType = await vscode.window.showQuickPick([
                    {
                        label: "Standard API token (no scopes)",
                        description: "Uses the Jira site URL directly.",
                        value: false,
                    },
                    {
                        label: "API token with Jira scopes",
                        description: "Uses the Atlassian API gateway and requires Jira scopes.",
                        value: true,
                    },
                ], {
                    title: "Atlassian MCP for Copilot: Select the API token type",
                    placeHolder: "Choose the same type selected when you created your API token.",
                });
                if (!tokenType) {
                    return;
                }
                apiTokenScoped = tokenType.value;
            }
            const token = await vscode.window.showInputBox({
                title: deployment === "cloud"
                    ? "Atlassian MCP for Copilot: Connect to Jira (3/3)"
                    : "Atlassian MCP for Copilot: Connect to Jira (2/2)",
                prompt: deployment === "cloud"
                    ? "Enter your Atlassian API token. It will be saved in VS Code only after the connection is verified."
                    : "Enter your Jira Personal Access Token. It will be saved in VS Code only after the connection is verified.",
                password: true,
                ignoreFocusOut: true,
                validateInput: (value) => (deployment === "cloud"
                    ? value.trim().length > 0
                    : /^[A-Za-z0-9._~+/-]+=*$/.test(value.trim()))
                    ? undefined : "Enter a valid token without spaces.",
            });
            if (token === undefined) {
                return;
            }
            const cloudUsername = username?.trim();
            let credentials: Credentials;
            if (deployment === "cloud") {
                if (!cloudUsername) {
                    throw new Error("Enter the Atlassian account email associated with your API token.");
                }
                credentials = {
                    url: normalizedUrl, deployment, username: cloudUsername,
                    apiToken: token.trim(), apiTokenScoped, revision: randomUUID(),
                };
            } else {
                credentials = { url: normalizedUrl, deployment, pat: token.trim(), revision: randomUUID() };
            }
            if (!await ensureInstalled()) {
                return;
            }
            await vscode.window.withProgress({
                location: vscode.ProgressLocation.Notification,
                title: "Verifying the Jira connection",
            }, async () => {
                await run(executable, [...isolatedArgs, "--no-env-file", "--check-connection"], storage,
                    output, {
                        ...process.env,
                        ...jiraEnvironment(credentials, vscode.workspace.getConfiguration("secureJiraMcp")),
                    }, 20_000);
                await context.secrets.store(jiraCredentialsKeys[credentials.deployment], JSON.stringify(credentials));
            });
            await context.globalState.update(activeConnectionKey, jiraConnectionKey(credentials.deployment));
            activeJiraCredentials = [credentials];
            activeConfluenceCredentials = undefined;
            changed.fire();
            if (!await requestMcpStart(previousActiveConnection)) {
                return;
            }
        } catch (error) {
            output.appendLine(String(error));
            const message = error instanceof Error ? error.message : "Unexpected error.";
            const action = await vscode.window.showErrorMessage(`Atlassian MCP for Copilot: ${message}`, "Show Output");
            if (action) {
                output.show();
            }
            return;
        } finally {
            settingUp = false;
        }
        const openChat = "Open Copilot Chat";
        const action = await vscode.window.showInformationMessage(
            "Jira credentials verified. VS Code was asked to start Atlassian MCP for Copilot. Review any trust prompt, then ask Copilot a Jira question.",
            openChat,
        );
        if (action === openChat) {
            try {
                await vscode.commands.executeCommand("workbench.action.chat.open");
            } catch (error) {
                output.appendLine(String(error));
                void vscode.window.showWarningMessage("Jira is connected, but Copilot Chat could not be opened. Open Chat from VS Code when available.");
            }
        }
    }

    async function activateSavedConnection(
        selection: ActiveConnectionKey,
        jiraCredentials: Credentials[],
        confluenceCredentials: ConfluenceCredentials | undefined,
    ): Promise<void> {
        const previousConnection = context.globalState.get<ActiveConnectionKey>(activeConnectionKey);
        if (!await ensureInstalled()) {
            return;
        }
        let selectedJiraConnections = jiraCredentials;
        let confluenceToActivate = confluenceCredentials;
        if (selection === "confluence" && confluenceToActivate) {
            confluenceToActivate = await verifySavedConfluenceCredentials(confluenceToActivate);
            if (!confluenceToActivate) {
                return;
            }
        } else {
            const selectedJira = jiraCredentials.find(
                (credentials) => jiraConnectionKey(credentials.deployment) === selection,
            );
            if (selectedJira) {
                const verifiedCredentials = await verifySavedJiraCredentials(selectedJira);
                if (!verifiedCredentials) {
                    return;
                }
                selectedJiraConnections = jiraCredentials.map((credentials) =>
                    credentials.deployment === verifiedCredentials.deployment ? verifiedCredentials : credentials);
            }
        }
        if (previousConnection === selection) {
            void vscode.window.showInformationMessage(
                `${connectionLabel(selection)} is connected and ready.`,
            );
            return;
        }
        await context.globalState.update(activeConnectionKey, selection);
        activeJiraCredentials = selectedJiraCredentials(selectedJiraConnections, selection);
        activeConfluenceCredentials = selectedConfluenceCredentials(confluenceToActivate, selection);
        changed.fire();
        await requestMcpStart(previousConnection);
    }

    async function verifySavedJiraCredentials(credentials: Credentials): Promise<Credentials | undefined> {
        try {
            await run(executable, [...isolatedArgs, "--no-env-file", "--check-connection"], storage,
                output, {
                    ...process.env,
                    ...jiraEnvironment(credentials, vscode.workspace.getConfiguration("secureJiraMcp")),
                }, 20_000);
            return credentials;
        } catch (error) {
            if (!isCredentialRejected(error)) {
                throw error;
            }
            const update = await vscode.window.showErrorMessage(
                "The saved Jira credential was rejected. Update it now?",
                "Update Token",
                "Cancel",
            );
            if (update !== "Update Token") {
                return undefined;
            }
            return updateJiraToken(credentials);
        }
    }

    async function updateJiraToken(credentials: Credentials): Promise<Credentials | undefined> {
        let username = credentials.username ?? "";
        let apiTokenScoped = credentials.apiTokenScoped ?? false;
        if (credentials.deployment === "cloud") {
            const enteredUsername = await vscode.window.showInputBox({
                title: "Atlassian MCP for Copilot: Update saved Jira credentials",
                prompt: "Confirm the Atlassian account email for this saved Jira site.",
                value: credentials.username ?? "",
                validateInput: (value) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
                    ? undefined : "Enter the Atlassian account email address.",
            });
            if (enteredUsername === undefined) {
                return undefined;
            }
            username = enteredUsername.trim();
        }
        const token = await vscode.window.showInputBox({
            title: "Atlassian MCP for Copilot: Update saved Jira credentials",
            prompt: credentials.deployment === "cloud"
                ? "Enter the replacement Jira API token."
                : "Enter the replacement Jira Personal Access Token.",
            password: true,
            ignoreFocusOut: true,
            validateInput: (value) => (credentials.deployment === "cloud"
                ? value.trim().length > 0
                : /^[A-Za-z0-9._~+/-]+=*$/.test(value.trim()))
                ? undefined : "Enter a valid token without spaces.",
        });
        if (token === undefined) {
            return undefined;
        }
        if (credentials.deployment === "cloud") {
            const tokenTypes: Array<vscode.QuickPickItem & { value: boolean }> = [
                { label: "Standard API token (no scopes)", value: false },
                { label: "API token with Jira scopes", value: true },
            ];
            const tokenType = await vscode.window.showQuickPick(tokenTypes, {
                title: "Atlassian MCP for Copilot: Select the replacement token type",
                placeHolder: "Choose the same type selected when you created the replacement token.",
            });
            if (!tokenType) {
                return undefined;
            }
            apiTokenScoped = tokenType.value;
        }
        const updated: Credentials = credentials.deployment === "cloud"
            ? {
                ...credentials,
                username,
                apiToken: token.trim(),
                apiTokenScoped,
                revision: randomUUID(),
            }
            : { ...credentials, pat: token.trim(), revision: randomUUID() };
        await vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: "Verifying the replacement Jira credential",
        }, async () => {
            await run(executable, [...isolatedArgs, "--no-env-file", "--check-connection"], storage,
                output, {
                    ...process.env,
                    ...jiraEnvironment(updated, vscode.workspace.getConfiguration("secureJiraMcp")),
                }, 20_000);
            await context.secrets.store(jiraCredentialsKeys[updated.deployment], JSON.stringify(updated));
        });
        return updated;
    }

    async function verifySavedConfluenceCredentials(
        credentials: ConfluenceCredentials,
    ): Promise<ConfluenceCredentials | undefined> {
        try {
            await run(executable, [...isolatedArgs, "--no-env-file", "--check-confluence-connection"], storage,
                output, {
                    ...process.env,
                    ...confluenceEnvironment(credentials, vscode.workspace.getConfiguration("secureJiraMcp")),
                }, 20_000);
            return credentials;
        } catch (error) {
            if (!isCredentialRejected(error)) {
                throw error;
            }
            const update = await vscode.window.showErrorMessage(
                "The saved Confluence credential was rejected. Update it now?",
                "Update Token",
                "Cancel",
            );
            if (update !== "Update Token") {
                return undefined;
            }
            return updateConfluenceToken(credentials);
        }
    }

    async function updateConfluenceToken(
        credentials: ConfluenceCredentials,
    ): Promise<ConfluenceCredentials | undefined> {
        let username = credentials.username ?? "";
        let apiTokenScoped = credentials.apiTokenScoped ?? false;
        if (credentials.deployment === "cloud") {
            const enteredUsername = await vscode.window.showInputBox({
                title: "Atlassian MCP for Copilot: Update saved Confluence credentials",
                prompt: "Confirm the Atlassian account email for this saved Confluence site.",
                value: credentials.username ?? "",
                validateInput: (value) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
                    ? undefined : "Enter the Atlassian account email address.",
            });
            if (enteredUsername === undefined) {
                return undefined;
            }
            username = enteredUsername.trim();
        }
        const token = await vscode.window.showInputBox({
            title: "Atlassian MCP for Copilot: Update saved Confluence credentials",
            prompt: credentials.deployment === "cloud"
                ? "Enter the replacement Confluence API token."
                : "Enter the replacement Confluence Personal Access Token.",
            password: true,
            ignoreFocusOut: true,
            validateInput: (value) => (credentials.deployment === "cloud"
                ? value.trim().length > 0
                : /^[A-Za-z0-9._~+/-]+=*$/.test(value.trim()))
                ? undefined : "Enter a valid token without spaces.",
        });
        if (token === undefined) {
            return undefined;
        }
        if (credentials.deployment === "cloud") {
            const tokenTypes = [
                { label: "Standard API token (no scopes)", value: false },
                { label: "API token with Confluence scopes", value: true },
            ];
            const tokenType = await vscode.window.showQuickPick(tokenTypes, {
                title: "Atlassian MCP for Copilot: Select the replacement token type",
                placeHolder: "Choose the same type selected when you created the replacement token.",
            });
            if (!tokenType) {
                return undefined;
            }
            apiTokenScoped = tokenType.value;
        }
        const updated: ConfluenceCredentials = credentials.deployment === "cloud"
            ? {
                ...credentials,
                username,
                apiToken: token.trim(),
                apiTokenScoped,
                revision: randomUUID(),
            }
            : { ...credentials, pat: token.trim(), revision: randomUUID() };
        const configuration = vscode.workspace.getConfiguration("secureJiraMcp");
        await vscode.window.withProgress({
            location: vscode.ProgressLocation.Notification,
            title: "Verifying the replacement Confluence credential",
        }, async () => {
            await run(executable, [...isolatedArgs, "--no-env-file", "--check-confluence-connection"], storage,
                output, {
                    ...process.env,
                    ...confluenceEnvironment(updated, configuration),
                }, 20_000);
            await context.secrets.store(confluenceCredentialsKey, JSON.stringify(updated));
        });
        return updated;
    }

    context.subscriptions.push(vscode.commands.registerCommand("secureJiraMcp.connect", () => connect(false)));
    void connect(true);

    async function connectConfluence(initialUrl?: string, setupAlreadyRunning = false): Promise<void> {
        if (settingUp && !setupAlreadyRunning) {
            void vscode.window.showInformationMessage("Atlassian MCP for Copilot setup is already running.");
            return;
        }
        settingUp = true;
        try {
            const previous = await getConfluenceCredentials(context);
            const previousActiveConnection = context.globalState.get<ActiveConnectionKey>(activeConnectionKey);
            const deployment = initialUrl
                ? jiraDeploymentForUrl(initialUrl, previous)
                : previous?.deployment ?? "data_center";
            const url = initialUrl ?? await vscode.window.showInputBox({
                title: "Atlassian MCP for Copilot: Connect to Confluence",
                prompt: deployment === "cloud"
                    ? "Enter your Confluence Cloud site URL (include /wiki if shown in the URL)."
                    : "Enter your Confluence Data Center or Server HTTPS base URL.",
                placeHolder: deployment === "cloud"
                    ? "https://company.atlassian.net/wiki"
                    : "https://confluence.example.com/confluence",
                ...(previous ? { value: previous.url } : {}),
                validateInput: (value) => jiraUrl(value)
                    ? undefined : "Enter an HTTPS base URL without credentials, a query, or a fragment.",
            });
            if (url === undefined) {
                return;
            }
            let username: string | undefined;
            if (deployment === "cloud") {
                username = await vscode.window.showInputBox({
                    title: "Atlassian MCP for Copilot: Connect to Confluence",
                    prompt: "Enter the Atlassian account email associated with your API token.",
                    ...(previous?.deployment === "cloud" && previous.username ? { value: previous.username } : {}),
                    validateInput: (value) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim())
                        ? undefined : "Enter the Atlassian account email address.",
                });
                if (username === undefined) {
                    return;
                }
            }
            const tokenTypes = [
                {
                    label: "Standard API token (no scopes)",
                    description: "Uses the Confluence site URL directly.",
                    value: false,
                },
                {
                    label: "API token with Confluence scopes",
                    description: "Uses the Atlassian Confluence API gateway and requires Confluence scopes.",
                    value: true,
                },
            ];
            let apiTokenScoped = false;
            if (deployment === "cloud") {
                const tokenType = await vscode.window.showQuickPick(tokenTypes, {
                    title: "Atlassian MCP for Copilot: Select the Confluence API token type",
                    placeHolder: "Choose the same type selected when you created your API token.",
                    ...(previous?.deployment === "cloud" && previous.apiTokenScoped !== undefined
                        ? { activeItem: tokenTypes[previous.apiTokenScoped ? 1 : 0] }
                        : {}),
                });
                if (!tokenType) {
                    return;
                }
                apiTokenScoped = tokenType.value;
            }
            const token = await vscode.window.showInputBox({
                title: "Atlassian MCP for Copilot: Connect to Confluence",
                prompt: deployment === "cloud"
                    ? "Enter your Atlassian API token. It is saved only after the connection is verified."
                    : "Enter your Confluence Personal Access Token. It is saved only after the connection is verified.",
                password: true,
                ignoreFocusOut: true,
                validateInput: (value) => (deployment === "cloud"
                    ? value.trim().length > 0
                    : /^[A-Za-z0-9._~+/-]+=*$/.test(value.trim()))
                    ? undefined : "Enter a valid token without spaces.",
            });
            if (token === undefined) {
                return;
            }
            const normalizedUrl = jiraUrl(url);
            if (!normalizedUrl) {
                throw new Error("Enter a valid HTTPS Confluence base URL.");
            }
            const cloudUsername = username?.trim();
            let credentials: ConfluenceCredentials;
            if (deployment === "cloud") {
                if (!cloudUsername) {
                    throw new Error("Enter the Atlassian account email associated with your API token.");
                }
                credentials = {
                    url: normalizedUrl, deployment, username: cloudUsername,
                    apiToken: token.trim(), apiTokenScoped, revision: randomUUID(),
                };
            } else {
                credentials = {
                    url: normalizedUrl, deployment, pat: token.trim(), revision: randomUUID(),
                };
            }
            if (!await ensureInstalled()) {
                return;
            }
            const configuration = vscode.workspace.getConfiguration("secureJiraMcp");
            await vscode.window.withProgress({
                location: vscode.ProgressLocation.Notification,
                title: "Verifying the Confluence connection",
            }, async () => {
                await run(executable, [...isolatedArgs, "--no-env-file", "--check-confluence-connection"], storage,
                    output, {
                        ...process.env,
                        ...confluenceEnvironment(credentials, configuration),
                    }, 20_000);
                await context.secrets.store(confluenceCredentialsKey, JSON.stringify(credentials));
            });
            await context.globalState.update(activeConnectionKey, "confluence");
            activeJiraCredentials = [];
            activeConfluenceCredentials = credentials;
            changed.fire();
            if (!await requestMcpStart(previousActiveConnection)) {
                return;
            }
        } catch (error) {
            output.appendLine(String(error));
            const message = error instanceof Error ? error.message : "Unexpected error.";
            const action = await vscode.window.showErrorMessage(`Atlassian MCP for Copilot: ${message}`, "Show Output");
            if (action) {
                output.show();
            }
            return;
        } finally {
            settingUp = false;
        }
        const openChat = "Open Copilot Chat";
        const action = await vscode.window.showInformationMessage(
            "Confluence credentials verified. VS Code was asked to start Atlassian MCP for Copilot. Review any trust prompt, then ask Copilot a Confluence question.",
            openChat,
        );
        if (action === openChat) {
            try {
                await vscode.commands.executeCommand("workbench.action.chat.open");
            } catch (error) {
                output.appendLine(String(error));
                void vscode.window.showWarningMessage("Confluence is connected, but Copilot Chat could not be opened. Open Chat from VS Code when available.");
            }
        }
    }
}

function jiraUrl(value: string): string | undefined {
    try {
        const url = new URL(value.trim());
        if (url.protocol === "https:" && url.hostname && !url.username && !url.password
            && !/[?#]/.test(value) && !value.includes("${")) {
            return url.href.replace(/\/+$/, "");
        }
    } catch (error) {
        if (!(error instanceof TypeError)) {
            throw error;
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

function isCredentialRejected(error: unknown): boolean {
    return error instanceof Error && /rejected the credential|authentication failed/i.test(error.message);
}

async function getJiraCredentials(context: vscode.ExtensionContext): Promise<Credentials[]> {
    const legacy = await context.secrets.get(legacyCredentialsKey);
    const legacyCredentials = legacy ? parseCredentials(legacy) : undefined;
    const results: Credentials[] = [];
    for (const deployment of ["cloud", "data_center"] as const) {
        const saved = await context.secrets.get(jiraCredentialsKeys[deployment]);
        const credentials = saved
            ? parseCredentials(saved)
            : legacyCredentials?.deployment === deployment ? legacyCredentials : undefined;
        if (credentials && !results.some(({ url }) => url === credentials.url)) {
            results.push(credentials);
        }
    }
    return results;
}

function parseCredentials(saved: string): Credentials {
    const parsed = JSON.parse(saved) as Credentials;
    return { ...parsed, deployment: parsed.deployment ?? "data_center" };
}

function credentialsSignature(credentials: Credentials[]): string {
    return credentials.map(({ deployment, revision }) => `${deployment}:${revision}`).join("|");
}

function jiraServerLabel(deployment: Credentials["deployment"]): string {
    return `${serverLabel} (${deployment === "cloud" ? "Cloud" : "Data Center"})`;
}

async function getConfluenceCredentials(context: vscode.ExtensionContext): Promise<ConfluenceCredentials | undefined> {
    const saved = await context.secrets.get(confluenceCredentialsKey);
    if (!saved) {
        return undefined;
    }
    const parsed = JSON.parse(saved) as ConfluenceCredentials;
    return { ...parsed, deployment: parsed.deployment ?? "data_center" };
}

function jiraEnvironment(
    credentials: Credentials,
    configuration: vscode.WorkspaceConfiguration,
): Record<string, string> {
    return {
        JIRA_URL: credentials.url,
        JIRA_DEPLOYMENT: credentials.deployment,
        JIRA_USERNAME: credentials.deployment === "cloud" ? credentials.username ?? "" : "",
        JIRA_API_TOKEN: credentials.deployment === "cloud" ? credentials.apiToken ?? "" : "",
        JIRA_API_TOKEN_SCOPED: credentials.deployment === "cloud" && credentials.apiTokenScoped ? "true" : "false",
        JIRA_PERSONAL_TOKEN: credentials.deployment === "data_center" ? credentials.pat ?? "" : "",
        JIRA_ALLOWED_HOSTS: "",
        JIRA_ALLOWED_NETWORKS: configuration.get<string>("allowedNetworks", "").trim(),
        JIRA_TRUST_HTTPS_PROXY: configuration.get<boolean>("trustHttpsProxy", false) ? "true" : "false",
        MCP_READ_ONLY: "true",
        MCP_CONFIRMATION_MODE: "elicit",
    };
}

function confluenceEnvironment(
    credentials: ConfluenceCredentials,
    configuration: vscode.WorkspaceConfiguration,
): Record<string, string> {
    return {
        CONFLUENCE_URL: credentials.url,
        CONFLUENCE_DEPLOYMENT: credentials.deployment,
        CONFLUENCE_USERNAME: credentials.deployment === "cloud" ? credentials.username ?? "" : "",
        CONFLUENCE_API_TOKEN: credentials.deployment === "cloud" ? credentials.apiToken ?? "" : "",
        CONFLUENCE_API_TOKEN_SCOPED: credentials.deployment === "cloud" && credentials.apiTokenScoped ? "true" : "false",
        CONFLUENCE_PERSONAL_TOKEN: credentials.deployment === "data_center" ? credentials.pat ?? "" : "",
        CONFLUENCE_ALLOWED_HOSTS: "",
        CONFLUENCE_ALLOWED_NETWORKS: configuration.get<string>("allowedNetworks", "").trim(),
        CONFLUENCE_TRUST_HTTPS_PROXY: configuration.get<boolean>("trustHttpsProxy", false) ? "true" : "false",
        MCP_READ_ONLY: "true",
        MCP_CONFIRMATION_MODE: "elicit",
    };
}

async function findPython(cwd: string): Promise<{ command: string; args: string[] } | undefined> {
    const candidates = process.platform === "win32"
        ? [{ command: "py", args: ["-3"] }, { command: "python", args: [] }]
        : [{ command: "python3", args: [] }];
    for (const candidate of candidates) {
        try {
            await run(candidate.command, [...candidate.args, "-I", "-c", pythonCheck], cwd, undefined, undefined, 5_000);
            return candidate;
        } catch (error) {
            if (!(error instanceof Error)) {
                throw error;
            }
        }
    }
    const selected = await vscode.window.showInputBox({
        title: "Atlassian MCP for Copilot: Python 3.11+ was not found",
        prompt: "Enter the absolute path to a Python 3.11+ executable, or install Python and retry.",
        validateInput: (value) => path.isAbsolute(value.trim()) ? undefined : "Enter an absolute path.",
    });
    if (!selected) {
        return;
    }
    const command = selected.trim();
    await run(command, ["-I", "-c", pythonCheck], cwd, undefined, undefined, 5_000);
    return { command, args: [] };
}

function run(command: string, args: string[], cwd: string, output?: vscode.OutputChannel,
    env?: NodeJS.ProcessEnv, timeout?: number): Promise<void> {
    return new Promise((resolve, reject) => {
        const child = spawn(command, args, { cwd, env, timeout, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
        const checkConnection = args.includes("--check-connection") || args.includes("--check-confluence-connection");
        const confluenceCheck = args.includes("--check-confluence-connection");
        let connectionDiagnostic = "";
        child.stdout.setEncoding("utf8").on("data", (chunk: string) => output?.append(chunk));
        child.stderr.setEncoding("utf8").on("data", (chunk: string) => {
            output?.append(chunk);
            if (checkConnection) {
                connectionDiagnostic = (connectionDiagnostic + chunk).slice(-8192);
            }
        });
        child.on("error", (error) => reject(checkConnection
            ? new Error(`Could not start the ${confluenceCheck ? "Confluence" : "Jira"} connection check.`)
            : error));
        child.on("close", (code, signal) => {
            if (code === 0) {
                resolve();
            } else if (checkConnection) {
                const service = confluenceCheck ? "Confluence" : "Jira";
                const prefix = `${service} connection could not be verified:`;
                const diagnostic = connectionDiagnostic
                    .split(/\r?\n/)
                    .map((line) => line.trim())
                    .find((line) => line.startsWith(prefix));
                const authenticationPrefix = `${service} authentication failed:`;
                const authenticationDiagnostic = connectionDiagnostic
                    .split(/\r?\n/)
                    .map((line) => line.trim())
                    .find((line) => line.startsWith(authenticationPrefix));
                reject(new Error(code === 3
                    ? authenticationDiagnostic?.slice(authenticationPrefix.length).trim()
                        || `${service} rejected the credential. Check the URL and token.`
                    : diagnostic?.slice(prefix.length).trim()
                        || `Could not verify ${service}. Check the URL, VPN, and corporate certificate or proxy settings.`));
            } else {
                reject(new Error(`${path.basename(command)} exited with ${code ?? signal}.`));
            }
        });
    });
}
