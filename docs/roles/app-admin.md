---
title: App Admin
---

# App Admin

The App Admin role (`app_admin`) manages users, teams and keys. A [Logos Admin](logos-admin.md#users) assigns this role in Logos. Ownership grants management access to a specific team.

The UI shows the role badge "App Admin" in the header menu.

## Account settings

Open the user menu and select **Account settings** to manage your personal passkeys.
Follow the shared [account settings procedure](app-developer.md#account-settings).

## Models

This page shows the model catalogue of the deployment: the name, the
description and the capabilities of each model. App Admins see the list. They
do not see the operator controls for each model (weights, aliases, add and
delete).

Use **Active models** to browse the catalogue. Use **Model requests** to request a model for your team.
Enter the Hugging Face repository name, select your team, and select **Vote for this model**.

![Models page](/img/roles/app-admin-models.png)

## Users

This page shows the users of the deployment, with their role, teams and
status. An App Admin can create users and assign them to teams. An App Admin
can create only App Developers, and no higher roles. The name and email of an
existing user can only be changed by a Logos Admin.

![Users page](/img/roles/app-admin-user-management.png)

## Teams

This page shows the teams, their owners and their members. For the teams that
they own, App Admins can create teams, add members and manage the API keys of
the team.

![Teams page](/img/roles/app-admin-team-management.png)

When you open a team, the page shows the detail view of the team. Owners and
Logos Admins see all the tabs that follow. Other members see only
**Overview** and **Members**.

### Overview

This tab shows the number of members, the active keys, the permitted models
and the budget use of the members. It also shows the defaults that apply when a
key or a member has no individual limit.

![Team detail — Overview](/img/roles/team-detail-overview.png)

### Members

When you open your own developer key, Logos can ask you to [choose a logging level](app-developer.md#choose-a-logging-level).

Open **Members → Import CSV** to add people to this team. **Add Member** remains available for a single account. The tab also shows owners and each person’s budget and rate-limit overrides.

![Team members and the Import CSV action](/img/roles/team-detail-members.png)

Choose a file, map the first name, last name and email columns, and select the rows to import. Existing accounts are matched by email, including Keycloak accounts. A repeated row leaves the membership and its developer key in place. Membership grants no global role or ownership.

![Mapped CSV rows and the selected team](/img/roles/team-members-csv.png)

In **Users → Import CSV** you can also choose one target team, map a team name column for assignments across teams, or import users without teams. Each team name must match an existing team you manage. An unknown team fails that row and other valid rows can still succeed. New users receive the App Developer role.

### Application Keys

This tab shows the application (service) keys of the team. You can create,
rotate and revoke keys. You can also set limits and model permissions for each
key.

Select **Show permissions** next to a key to see which models it can use and which providers serve each model.
In **Members**, expand a developer key to see the same list.

![Application Keys with the reachable models and providers expanded](/img/roles/team-detail-application-keys.png)

#### API Key Model Permissions

The model and provider permissions of the team are the default for the API
keys of the team. Defaults also include synced Azure deployments when a [Logos Admin enables automatic access](logos-admin.md#teams) for the team and provider.
Custom grants may include other catalog entries within the team’s privacy setting.

| **Override Team Permissions** | Models and providers that the key can use |
|-------------------------------|-------------------------------------------|
| Off | The key uses the team permissions. If the team permissions change, then the key permissions also change. |
| On | The key uses its own providers and models. You can select providers and models that the team permissions do not include. |

Use custom permissions for a key that needs more models than the team
default, for example a production key. A team owner can set custom
permissions on each key of the team. If you remove models or providers from
the team permissions, then the custom permissions of the keys do not change.
If you remove a provider from a key, then the key also loses the models of
that provider.

In the key settings, the model list shows all models that the selected
providers serve. The **Models** page shows the models that you can use now.

### Repositories

This tab shows the GitHub repositories that contain the applications of the
team. Owners link a repository URL, a branch and optional path filters. For
private repositories, owners can paste a read-only deploy key. Only one workflow analysis per repository can be in progress at a time.
**Automatic workflow analysis** is on by default. It includes the repository in the first analysis, nightly checks for new commits, and bulk analyses.
Clear the checkbox to cancel queued automatic analyses. Analyses that already started can finish.
Use **Queue agent analysis** to request one analysis at any time. The coding agent has a separate setting.

![Team detail — Repositories](/img/roles/team-detail-repositories.png)

#### Enable the Coding Agent

Repository analysis uses read-only access. The coding agent can also change
files and create pull requests after a repository owner requests work on GitHub.

1. Give the configured agent account access to the repository on GitHub.
2. Select **Edit repository link** for the repository.
3. Select **Enable LogosOSSAgent for this repository**.
4. Enter the **Build and test command** that the agent must run from the repository root.
5. Optionally enter **Repository instructions** for the tools and conventions of the project.
6. Select **Save**.

![Team detail — Repository coding agent settings](/img/roles/team-detail-repository-agent.png)

The example enables the coding agent and disables **Automatic workflow analysis**.
The **Branch** is the base for new issue work. The command runs without
credentials or internet access. The required tools and dependencies must exist
in the agent image or in the workspace cache (see below). A failed command
permits only a draft pull request.

Assign an issue or pull request to the agent on GitHub to request work.
You can also request a review or mention the agent in a comment.
The agent checks enabled repositories every 2 minutes. Analysis deploy keys
remain read-only and do not give the coding agent access.

The list shows **Coding agent: Enabled** when the option is active.
Clear the option and select **Save** to stop new requests.
Queued work and retries keep their saved repository and command.
If several teams enable the same repository, the oldest enabled link supplies
the settings. Linked repositories do not use Logos deployment workflows.

##### Agent Sandbox Permissions

Three settings in the coding agent section control access outside the
sandbox. Only a Logos Admin can change them:

| Setting | Effect |
|---|---|
| **Allow unrestricted internet access (HTTPS to any public host)** | If selected, agent sessions for this repository can reach all public HTTPS hosts. Logos then does not use the list of allowed network hosts. This setting is off by default. |
| **Allowed network hosts** | The HTTPS hosts that agent sessions for this repository can reach, for example to download toolchains. Write one host per line. If the list is empty, then sessions have no internet access. |
| **Let LogosOSSAgent comment on issues and pull requests during a session** | If selected, the agent can comment on issues and pull requests of this repository while a session runs. The comments use the GitHub account of the agent. |

The screenshot in [Enable the Coding Agent](#enable-the-coding-agent) shows
the settings as a Logos Admin sees them. An App Admin sees them in the
read-only block **Agent sandbox permissions** of the dialog. If a link has
these permissions, then the list shows, for example, **Network: 2 hosts** (or
**Network: Any public host**) and **Comments during sessions: Allowed**.

A host is a DNS name, for example `github.com`. A leading `*.` permits all
subdomains, for example `*.githubusercontent.com`. Logos refuses a scheme, a
port, a path, an IP address, `localhost` and a top-level wildcard such as
`*.com`. The list holds a maximum of 64 hosts. Logos always blocks private and
internal addresses.

Select unrestricted internet access only if a list of hosts is not
practical. The agent reads issues and comments that other persons write. If
the agent has unrestricted internet access, then such text can make the agent
send repository content or session data to a server. Also with this setting,
sessions can use only HTTPS on port 443 to public addresses, and only while
the session runs.

Changing the repository URL of a link resets the three settings, because they
were granted for the previous repository. Logos ignores the three settings in
the same save. To grant them for the new repository, edit the link again
after the save.

Every session on a linked repository also gets the following, independent of
the three settings:

- **Workspace cache.** `$LOGOS_CACHE_DIR` (`/cache`) keeps toolchains and
  downloaded dependencies between the sessions of a workspace. The build and
  test command can read it, but it still runs without credentials or internet
  access.
- **GitHub reads.** The command `logos-github` in the session reads issues
  and pull requests of the repository. With the comment permission it also
  posts comments, for example to claim an issue as the contribution guide of
  the repository asks.
- **Fork.** If the agent account cannot push to the repository, the agent
  pushes to its own fork and opens the pull request from there.
- **Closing references.** The pull request closes the issues that the agent
  names, but only open issues that are assigned to the agent account.
- **Model disclosure.** Every comment, review and pull request description of
  the agent names the model that wrote it.

An App Admin sees the two settings read-only in the edit dialog, as
**Network access** and **Comments during sessions**. To change them, ask a
Logos Admin. The list shows **Network: N hosts** if the repository permits
hosts, and **Comments during sessions: Allowed** if the agent can comment.

### Workflows

This tab shows the latest AI-workflow analyses for the linked repositories of
the team. It shows Mermaid diagrams of the flows that the analysis found. It
also shows recommendations for each call: the **SLO** and the **objective
priority** (the order of latency, quality and price). Owners and Logos Admins
can **edit** the Mermaid diagram of a workflow. Later analyses keep that edit.
If the agent draws a different diagram, the tab shows an **Agent update** next
to the edit. Then you can select **Accept** for the proposal or **Keep mine**.
Owners can also **Accept** a recommendation, **Override** the SLO or the
priority order, or **Reject** the recommendation.

The **Application key** picker above the recommendations applies to every
Accept and Override on the tab. The default is the key of the team with the
highest priority (usually the production key). If you select **No key**, the
key priorities do not change. When you accept or override a recommendation,
Logos sets the queue priority of that key from the confirmed SLO. Thus the
orchestrator serves the traffic as the SLO requires.

A new analysis **proposes** changes and does not overwrite your decisions. If
it recommends what you already accepted, overrode or rejected for a call site,
Logos keeps your decision ("Kept from the previous analysis"). If it
recommends something different, the call site is **pending** again. The tab
then shows the earlier decision next to the new proposal ("Was accepted:
ux-critical · latency › quality › price"). The key priorities change only
after you review the proposal. A decision stays valid through analyses that you
did not review. The tab shows the earlier decision, and Logos carries it over.
If a newer analysis arrives while the tab is open, Logos refuses the review of
an outdated proposal. Then reload the tab.

The analysis often cannot find which model a call site uses, because this
information is usually in the configuration and not in the code. Thus the
**Model** column is a picker. Select the model that the call site uses, or
leave the value **Unknown**. Later analyses keep your selection. If the model
has Likert profile ratings, the tab shows a spider chart next to it.

![Team detail — Workflows](/img/roles/team-detail-workflows.png)

### Models

This tab shows which catalogue models the team can use.

![Team detail — Models](/img/roles/team-detail-models.png)

### Activity

This tab shows the live queue state of the team, the recent request log, the
token totals and the export function.
Under **Requests**, open **Everyone in this team** and type part of a requester’s name.
Select a result to show that person’s requests.

![Team activity with the request log filtered to one requester](/img/roles/team-detail-activity.png)

### Cloud Usage

This tab shows the charges of the cloud providers for the off-site traffic of
the team. Local models do not show here. For local models, see Activity.

![Team detail — Cloud Usage](/img/roles/team-detail-cloud-usage.png)

### Settings

This tab shows the monthly budget of the team, the default key budget and the
default cloud and local rate limits. An owner can also delete the team here.
The **Team privacy** section shows where the team’s requests can be processed. A Logos Admin can change it. See [Team privacy](logos-admin.md#team-privacy).

Under **Public stats** you decide whether the team appears on the public
statistics page (`/usage`). There you can also give the team a **Category**,
for example "Research" or "Teaching". The public page groups teams by this
label. Select an existing category from the list, explicitly create a new one, or
select **Uncategorized** to clear it. Apply these choices with **Save Public Stats**
in the same section; **Save Settings** below only saves the budget and rate limits.
A confirmation appears after saving. The public page refreshes periodically, so
changes may take up to 30 minutes to appear with the default cache setting.

The **Local requests per hour** chart counts arrivals routed to local Logos
workers, regardless of the response status. It excludes cloud requests and
requests with no known route. The chart is an exception to the Public Stats
setting: it counts all teams but does not identify individual teams or workers.
Other public usage figures still use only published teams.

The chart shows the last 7 days with at least a 24-hour delay, in 1-hour
intervals. Use **Earlier 24h** or **Later 24h** to change the displayed period.
Select **Include Logos Agent** or **Include benchmarks** to add that traffic.
Both options are off by default. This chart does not show GPU utilization.

![Public stats page with hourly local request arrivals](/img/roles/public-stats-hourly-load.png)

Provider batches do not reserve budget. At batch creation, Logos checks only costs that it already settled.
A provider batch counts against the budget only after it finishes and Logos settles its costs.
Thus, several batches can start with the same budget left and exceed the key or team budget.
See [Batch budget checks](../batch-processing.md#permission-ownership-and-budget-checks).

![Team detail — Settings](/img/roles/team-detail-settings.png)

## My Workspace

This page is the developer view of the signed-in user. It shows the teams of
the user and the API keys of the user. You can create, rotate and revoke keys.
You can also set model permissions for each key. Every role that has at least
one team and one key can use this page.

![My Workspace page](/img/roles/app-admin-my-workspace.png)

## AI Tools

This is the same guided setup as for every other role. Select a coding
assistant, a team key and a model. Then install the assistant and connect it.
The page does not change with the role. For the step-by-step procedure, see
[App Developer → AI Tools](app-developer.md#ai-tools).

## Batches

This page is the OpenAI Batch API in the browser. Upload a `.jsonl` file, look
at the progress of the job and download the result file. For the server-side
process, see [batch processing](../batch-processing.md). The page is the same
for every role that can open it (App Admin and Logos Admin).

In **New batch**, select your developer key and an input file. If you need
help creating the file, select **Copy the batch setup prompt** under the file picker.
The prompt guides the agent through preparing the file and estimating costs
without submitting a batch.

The form counts requests per model and estimates the cost from recent
successful requests on the selected key. Automatic mode shows separate
estimates for provider batch and Logos replay. If any model lacks enough
history for a mode, its estimate is unavailable. Actual cost can be higher:
the estimate does not set a spending cap or reserve budget.

![Batches page with separate provider batch and Logos replay estimates](/img/roles/batches.png)
