![Python](https://a11ybadges.com/badge?logo=python)
![Roblox](https://a11ybadges.com/badge?logo=roblox)

# RoFinder

Find potentially unclaimed Roblox groups by checking group IDs. RoFinder sends qualifying groups to a Discord webhook and includes a local dashboard for scan activity and settings.

## Setup

You need Python 3.10 or newer and Git.

```powershell
git clone https://github.com/RXNationGMG/RoFinder.git
```

```powershell
cd RoFinder
```

```powershell
python -m pip install -r requirements.txt
```

Open `.env` and replace `PASTE_NEW_WEBHOOK_URL_HERE` with your Discord webhook URL.

## Run

```powershell
python main.py
```

Use the console menu to start or stop scanning and edit settings. Open the dashboard URL printed in the console for live logs, group details, and settings. Scanning starts only after you choose **Start scanning**.

The configured group ID range is checked once per scan, beginning at a randomized point. The default range is 1,000,000 to 9,999,999; you can change it in the dashboard. Roblox does not provide a public API that lists every unclaimed group, so finding one is not guaranteed.

*RoFinder | By: RXNation*