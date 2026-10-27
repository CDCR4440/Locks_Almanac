# Colofornia Dreaming — website embed

This repository keeps a small page up to date with the [Colofornia Dreaming Substack](https://coloforniadreaming.substack.com). The page lists recent posts and includes Substack’s subscribe box. It is meant to sit inside the GoDaddy site at coloforniadreaming.com as a window (an iframe).

The page has no frame-busting code. Styling is inside the page itself. Clicking a title opens that post on Substack in a new browser tab, so the rest of the website stays put.

## 1. Turn on GitHub Pages (one time)

Do this after the embed change is merged into the `main` branch.

1. Open [github.com/CDCR4440/Locks_Almanac](https://github.com/CDCR4440/Locks_Almanac).
2. Click **Settings**.
3. In the left sidebar, click **Pages**.
4. Under **Build and deployment**, set **Source** to **GitHub Actions**.
5. You do not pick a branch. Leave the page.

Merging this change starts a publish job on its own. That first job can fail if Pages is not set to **GitHub Actions** yet. A red X on that first run is expected. Set the source as above, then run the job by hand:

1. Click the **Actions** tab.
2. Click **Publish Substack embed** in the left sidebar.
3. Click **Run workflow**, choose **main**, then click the green **Run workflow** button.

Wait until the job has a green check. The page can take a few minutes to appear the first time. After later updates, GitHub may keep the previous copy cached for about 10 minutes.

## 2. The live address

**https://cdcr4440.github.io/Locks_Almanac/**

The same page is also at **https://cdcr4440.github.io/Locks_Almanac/embed.html**. Either address can be used in the iframe. Use the first one.

## 3. Paste this into GoDaddy Website Builder

1. Edit the page on coloforniadreaming.com.
2. Add an **HTML** section where the post list should appear.
3. Paste this one line:

```html
<iframe src="https://cdcr4440.github.io/Locks_Almanac/" title="Colofornia Dreaming latest posts" width="100%" height="600" style="border:0;width:100%;max-width:100%;" loading="lazy"></iframe>
```

4. Set **Forced Height** to **600**. Anywhere from **520** to **600** also works. That height keeps the post list on screen. The subscribe box is on the same page, under the list, so a visitor scrolls inside the frame to reach it. To show the list and the subscribe box together without that scroll, set **Forced Height** to **900**.

The list shows the latest 10 posts when the feed has that many. The feed has fewer than 10 right now, so the page shows every post it contains. The subscribe box under the list is Substack’s own embed (`https://coloforniadreaming.substack.com/embed`).

## What updates by itself

A job named **Publish Substack embed** runs every 6 hours (UTC). It downloads `https://coloforniadreaming.substack.com/feed` directly and republishes the page. It also runs when `main` changes, and when you use **Run workflow**.

The job does not use rss2json or any other third-party RSS proxy.

If nobody commits to this repo for about 60 days, GitHub pauses the 6-hour schedule. The last published page stays up. Open **Actions**, run **Publish Substack embed** once, and the schedule starts again.

## If the page is blank or a post is missing

1. Open the **Actions** tab and look at the newest **Publish Substack embed** run.
2. A red X means the Substack feed could not be downloaded. The previous page stays up. Click **Run workflow** to try again.
3. After a green check, wait about 10 minutes, then refresh the website.

## For a developer

- `scripts/build_site.py` downloads the feed and writes `site/index.html` and `site/embed.html`. It uses the Python standard library only.
- `python3 scripts/build_site.py --self-test` checks parsing and HTML escaping without the network.
- `python3 scripts/build_site.py` rebuilds the snapshot in `site/`.
- `.github/workflows/pages.yml` rebuilds the page on the runner. Pull requests only build it. Pushes to `main`, the 6-hour schedule, and **Run workflow** also deploy the `site` folder with GitHub Pages (`actions/deploy-pages`). The live site is that deployed copy. It can be newer than the snapshot committed in git.
- Change `MAX_POSTS` in `scripts/build_site.py` if the list should be longer than 10.
