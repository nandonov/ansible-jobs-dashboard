# Run the dashboard with macOS 26 native containers

Apple ships a first-party container stack for macOS 26 (and newer) that replaces Docker Desktop. This guide shows how to run the Ansible Job Dashboard locally with that toolchain using the `container` CLI, including persistent storage for the SQLite database.

## Prerequisites

- An Apple silicon Mac running macOS 26 beta or newer.
- The [`container` CLI](https://github.com/apple/container) installed from the latest signed release package.
- Xcode Command Line Tools (required by the installer).
- A shell with administrative access. Some steps may prompt for sudo.

Once the package is installed, start the system services:

```bash
container system start
```

The first run prompts you to install the recommended Linux kernel image—answer **Y** to allow the download. Confirm everything is healthy with:

```bash
container system status
```

> ℹ️ The dashboard has only been validated on Apple silicon hosts. The container runtime is not available on Intel Macs.

## Build the backend and frontend images

From the repository root:

```bash
cd ansible-job-dashboard

# Build the FastAPI backend image
cd backend
container build --tag local/ansible-dashboard-backend:latest .

# Build the React frontend image
cd ..
cd frontend
container build --tag local/ansible-dashboard-frontend:latest .

# Return to the repository root
cd ..
```

Re-run the corresponding `container build` command after making code changes to either service.

## Create shared resources

Create a dedicated network so the two services can reach each other, and a named volume for the SQLite database:

```bash
container network create ansible-dashboard || true
container volume create ansible-dashboard-backend-data || true
```

The `|| true` suffix keeps the commands from failing if the resources already exist.

## Run the backend service

Launch the FastAPI backend, publishing port 8000 to the host and mounting the persistent data volume. The container name **must be `backend`** so the frontend's NGINX config can reach it by DNS.

```bash
container run \
  --name backend \
  --network ansible-dashboard \
  --detach \
  --publish 127.0.0.1:8000:8000 \
  --volume ansible-dashboard-backend-data:/app/data \
  --env BACKEND_CORS_ORIGINS=http://localhost:3000 \
  --env DATABASE_URL=sqlite:///data/database.db \
  local/ansible-dashboard-backend:latest
```

The backend runs as the non-root `appuser`. The first start creates `/app/data/database.db` inside the mounted volume, so subsequent runs retain the job history.

Check the logs if you need to troubleshoot:

```bash
container logs backend --follow
```

## Run the frontend service

Start the production React bundle served by NGINX on port 3000:

```bash
container run \
  --name frontend \
  --network ansible-dashboard \
  --detach \
  --publish 127.0.0.1:3000:80 \
  local/ansible-dashboard-frontend:latest
```

Because both containers share the `ansible-dashboard` network, NGINX resolves the backend at `http://backend:8000` without additional configuration.

Visit <http://localhost:3000> to confirm the dashboard is available.

## Managing the stack

- List running services: `container list`
- Restart a service: `container stop <name> && container start <name>`
- Stop the stack:
  ```bash
  container stop frontend backend
  container delete frontend backend
  ```
- Remove the persistent data (if you want a clean slate):
  ```bash
  container volume delete ansible-dashboard-backend-data
  ```

The named volume persists automatically between restarts, so you do **not** need to recreate it every time.

## Updating to a new build

1. Stop the running containers (`container stop frontend backend`).
2. Rebuild the image that changed (`container build --tag ...`).
3. Start the service again with the `container run` command from above.

If you want to keep the commands handy, drop them into a shell script or alias; the CLI is fully scriptable.

## Additional resources

- [`container` tutorial](https://github.com/apple/container/blob/main/docs/tutorial.md)
- [`container` command reference](https://github.com/apple/container/blob/main/docs/command-reference.md)
- [`container` how-to guide](https://github.com/apple/container/blob/main/docs/how-to.md)

These documents cover networking, image management, and troubleshooting in more depth should you need advanced scenarios (custom kernels, registry auth, etc.).
