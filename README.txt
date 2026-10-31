REEL GRAB BACKEND
1. Upload ALL these files (server.py, requirements.txt, Dockerfile, render.yaml) to a new GitHub repository, at the top level.
2. Render.com > New > Web Service > pick the repo > Runtime: Docker > Plan: Free > Deploy.
3. When done, open your Render address in a browser. You should see {"status":"ok"}.
4. That address is your backend URL. Paste it into the website's Server settings.
Optional: add environment variable ALLOWED_ORIGIN = https://YOUR-SITE.netlify.app
