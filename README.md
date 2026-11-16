# Orange Watch

Open-source news and alerts for Orange, California, reaching out to the world in five tiers:
Orange & OC, SoCal, California, United States, and World (45 public feeds, listed in `sources.json`).

**Dashboard:** https://emmanuelh-droid.github.io/orange-watch/

## How it updates

- The **Refresh feeds** workflow (`.github/workflows/refresh.yml`) runs every 10 minutes.
  GitHub sometimes starts scheduled runs a few minutes late.
- Each run re-checks every **Orange & OC** source and keeps every local story for 7 days.
  The **other tiers** refresh once an hour.
- A **manual run** refreshes every tier at once. The page shows the new data 2–3 minutes later.
- GitHub turns off scheduled workflows in a public repo after 60 days with no commits, and emails
  you first. To turn it back on, open the Actions tab, then **Refresh feeds**, then **Enable workflow**.

## Refresh by hand

- **On the dashboard:** tap **Refresh on GitHub**, then **Run workflow**, then **Run workflow** again.
- **In the GitHub app:** open the repo, tap **Actions**, then **Refresh feeds**, then **Run workflow**.
- **With the iPhone Shortcut:** one tap, or say "Hey Siri, Refresh Orange Watch". Set it up below.

## iPhone Shortcut

### 1. Make a GitHub token that can only start this workflow

1. Open https://github.com/settings/personal-access-tokens/new. On a phone, use Safari and sign in if asked.
2. **Token name:** `Orange Watch Shortcut`. **Expiration:** pick one, such as 1 year.
3. **Repository access:** choose **Only select repositories**, then pick `orange-watch`.
4. **Permissions:** under Repository permissions, set **Actions** to **Read and write**. Leave everything else alone.
5. Tap **Generate token** and copy it. GitHub shows it only once.

### 2. Build the Shortcut

In the Shortcuts app, tap **+** and add these actions in order:

1. **Text:** paste your token. Rename the action's output to `Token`: tap the output, then **Rename**.
2. **Get Contents of URL**
   - URL: `https://api.github.com/repos/emmanuelh-droid/orange-watch/actions/workflows/refresh.yml/dispatches`
   - Tap **Show More**, then set **Method** to **POST**.
   - **Headers.** Add three:
     - `Authorization`: type `Bearer `, with a space after it, then insert the `Token` variable.
     - `Accept`: `application/vnd.github+json`
     - `X-GitHub-Api-Version`: `2022-11-28`
   - **Request Body:** **JSON**. Add two fields:
     - `ref` (Text): `main`
     - `inputs` (Dictionary). Inside it, add `all_tiers` (Text): `true`
3. **If:** set the input to **Contents of URL** and the condition to **does not have any value**.
   - Inside **If**, add **Show Notification**: `Orange Watch is refreshing. New data in 2-3 minutes.`
   - Inside **Otherwise**, add **Show Notification** with **Contents of URL** in the message. GitHub's error explains what went wrong.
4. Optional, after **End If**: add **Open URLs** with `https://emmanuelh-droid.github.io/orange-watch/`.

Name the Shortcut **Refresh Orange Watch**. To put it on your Home Screen, tap the share button, then **Add to Home Screen**.

A successful request returns nothing, which is why "does not have any value" means it worked.
If the token expires or is revoked, make a new one and paste it into the **Text** action.

## Run it on a Mac instead

```bash
python3 server.py
```

This opens http://localhost:8787. On the Mac, Orange & OC is re-checked about every minute.
