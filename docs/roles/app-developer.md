---
title: App Developer
---

# App Developer

New accounts receive the App Developer role (`app_developer`) when they sign in. Logos Admins manage roles in Logos. App Developers use the models of their team with their API keys.

The UI shows the role badge "App Developer" in the header menu. A user can
use the developer-only pages only if the user belongs to a team that has an
API key. Until then, the UI shows a **No Access** page.

## Account settings

Every authenticated user can manage personal passkeys, including users without a team or API key.
The procedure is the same for App Developers, App Admins, and Logos Admins.

1. Open the user menu at the bottom of the navigation.
2. Select **Account settings**.

On a mobile device, open the navigation first.
The user menu contains **Account settings** and **Log out**.

![User menu with Account settings](/img/roles/account-settings-menu.png)

The **Passkeys** list shows each passkey name and the date it was added.
The list belongs to your central account. It can include passkeys added through another application.
If you have no passkeys, the page shows **No passkeys yet**.

![Account settings — Passkeys](/img/roles/account-settings-passkeys.png)

### Add a passkey

1. Select **Add passkey**.
2. Enter a distinct **Passkey name**, such as `Personal laptop` or `Backup security key`.
3. Select **Add passkey** in the dialog.
4. Follow the browser or device instructions.

The browser can ask for a fingerprint, face check, device PIN, or security key.
The new passkey appears in the list after the account service saves it.
If you cancel the browser prompt, you can try again from the dialog.

![Account settings — Add passkey dialog](/img/roles/account-settings-add-passkey.png)

### Remove a passkey

**CAUTION:** Removal affects your central account and all applications that use this passkey.
Make sure another passkey or your usual TUM sign-in method is available.

1. Select **Remove** next to the passkey.
2. Check the passkey name and the account warning.
3. Select **Remove passkey** to confirm, or **Cancel** to keep it.

The removed passkey disappears from the list. Other passkeys remain available.
An account policy can disable removal. If an error occurs, the page keeps the passkey visible and shows an explanation.

![Account settings — Remove passkey confirmation](/img/roles/account-settings-remove-passkey.png)

If the list cannot load, select **Try again**.
If the error remains, contact your Logos administrator. See [Passkey Login](../passkey-login.md#account-api-access) for the required settings.

## Models

This page shows the model catalogue of the deployment. For each model, it
shows what the model supports (function calling, vision, reasoning) and where
Logos serves the model. You can search the list and open the details of a
model.

Use **Active models** to browse the catalogue. Use **Model requests** to request a model for your team.
Enter the Hugging Face repository name, select your team, and select **Vote for this model**.

![Models page](/img/roles/app-developer-models.png)

## My Workspace

This page is the own view of the developer. It shows the teams of the user and
the API keys in these teams. On this page, you can create, rotate and revoke
keys. You can also set model permissions for each key. Use this page to get the
secret that you need to call the [Logos API](../user/api-usage.md).

A key without custom permissions uses the model permissions of its team. A key
with **Override Team Permissions** uses its own providers and models. These can
include models that the team permissions do not include. The key settings show
all models of the selected providers. The **Models** page shows the models that
you can use now. For more information, see
[API Key Model Permissions](app-admin.md#api-key-model-permissions).

![My Workspace page](/img/roles/app-developer-my-workspace.png)

Monthly member budgets and key budgets apply to cloud usage.
If a budget reaches its limit, including $0, the key can still use permitted local models.
Local rate limits and model permissions still apply.
The budget message shows this distinction; it does not mean that the key is inactive.

![My Workspace — exhausted cloud budget with local models available](/img/roles/app-developer-my-workspace-budget.png)

### Choose a logging level

1. Open **My Workspace**.
2. Find your developer key in the team card.
3. Select **Expand models**.

If the key uses billing-only logging, then Logos asks for your choice the first time you open it.

![My Workspace — choose a logging level for your developer key](/img/roles/my-workspace-logging-choice.png)

- **Keep billing-only logging** keeps usage and billing logs.
- **Enable full logging** lets Logos store requests and responses for this key. Logos can use this content in benchmarks.

Logos saves the choice for you and the team shown in the dialog. A different team has a separate choice.
Select **Decide later** to open the key and make your choice the next time you open it.
To change the logging level later, select **BILLING** or **FULL** on the key card.

## AI Tools

**AI Coding Tools** is a guided wizard that every role uses. The page does not
change with the role badge. The wizard helps you to connect a coding
assistant, Claude Code or OpenCode, to Logos with your own API key. The step
numbers change to fit your setup. The wizard skips **Team** if you have only
one key. The wizard skips **Model** if that key can reach only one usable
model.

### 1. Tool

Select Claude Code or OpenCode. The comparison table helps you to decide. Both
tools use the same Logos models and the same key. They are different in these
ways: where they run, how they use the context window, and what they do to an
existing setup.

![AI Tools — choose tool](/img/roles/ai-tools-step-tool.png)

### 2. Team

Select the team whose key the assistant uses. The billing and the model
permissions follow that key. The wizard shows this step only if you have more
than one key.

![AI Tools — choose team](/img/roles/ai-tools-step-team.png)

### 3. Model

Select the model that the assistant calls. For Claude Code, the wrapper maps
every alias (`opus` / `sonnet` / `haiku`) to this one model. Thus `/model`
does not send a request outside Logos.

![AI Tools — choose model](/img/roles/ai-tools-step-model.png)

### 4. Install

This step shows the install commands for the tool for each operating system.
Do not do this step if the tool is already installed. The tabs show macOS,
Linux and Windows.

![AI Tools — install](/img/roles/ai-tools-step-install.png)

### 5. Connect

This step shows generated commands that connect the tool to this Logos
deployment. For Claude Code, the commands install the `claude-logos` wrapper.
The wrapper does not change your normal `claude` Anthropic setup. `WebSearch`
works in these sessions. Logos does the searches on DuckDuckGo, and you do not
need an Anthropic account.

![AI Tools — connect](/img/roles/ai-tools-step-connect.png)

### 6. Verify

In this step, you can check the connection (`claude-logos --check`). You can
also update the wrapper or uninstall it.

![AI Tools — verify](/img/roles/ai-tools-step-verify.png)
