module.exports = {
  apps: [
    {
      name: "herald-v2",
      cwd: "/root/herald-v2",
      script: "/usr/bin/python3",
      args: "-m chainlit run app.py --host 0.0.0.0 --port 8002",
      interpreter: "none",
      env: {
        PYTHONPATH: "/root/herald-v2/.python-packages",
        PYTHONUNBUFFERED: "1",
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
    },
  ],
};

