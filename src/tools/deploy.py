"""One-Click Deploy tool — wrap docker-compose for production deployment.

Agents call this to trigger ``docker compose up`` / ``down`` operations
and get back structured status + log output.

The tool validates the compose file exists before executing.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from src.core.schema import DeployResult


class DeployManager:
    """Manage docker-compose deployments.

    Args:
        compose_path: Path to ``docker-compose.yml``.
        default_services: Services to deploy when none are specified.
    """

    def __init__(
        self,
        compose_path: str | Path = "docker/docker-compose.yml",
        default_services: list[str] | None = None,
    ) -> None:
        self._compose_path = str(Path(compose_path).resolve())
        self._default_services = default_services or []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def up(
        self,
        services: list[str] | None = None,
        build: bool = True,
        detach: bool = True,
        timeout: float = 120.0,
    ) -> DeployResult:
        """Start services via ``docker compose up``.

        Args:
            services: Specific services to start. None = all.
            build: Rebuild images before starting.
            detach: Run containers in background.
            timeout: Maximum wait time in seconds.

        Returns:
            ``DeployResult`` with status and log output.
        """
        services = services or self._default_services
        return await self._run_compose(
            ["up"] + (["--build"] if build else []) + (["-d"] if detach else []) + services,
            timeout=timeout,
        )

    async def down(
        self,
        volumes: bool = False,
        timeout: float = 60.0,
    ) -> DeployResult:
        """Stop and remove services via ``docker compose down``.

        Args:
            volumes: Also remove named volumes.
            timeout: Maximum wait time.

        Returns:
            ``DeployResult`` with status.
        """
        cmd = ["down"]
        if volumes:
            cmd.append("--volumes")
        return await self._run_compose(cmd, timeout=timeout)

    async def status(self, timeout: float = 30.0) -> DeployResult:
        """Check service status via ``docker compose ps``.

        Returns:
            ``DeployResult`` with container status in ``log``.
        """
        return await self._run_compose(["ps"], timeout=timeout)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_compose(
        self, args: list[str], timeout: float
    ) -> DeployResult:
        """Execute a docker compose command and return structured result."""
        compose_file = Path(self._compose_path)
        if not compose_file.exists():
            return DeployResult(
                service_name=",".join(args[-1:]) if args else "all",
                status="failed",
                log=f"Compose file not found: {self._compose_path}",
            )

        cmd = ["docker", "compose", "-f", self._compose_path] + args

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            log_parts: list[str] = []
            if stdout:
                log_parts.append(stdout.decode("utf-8", errors="replace"))
            if stderr:
                log_parts.append(stderr.decode("utf-8", errors="replace"))
            log = "\n".join(log_parts).strip()

            if proc.returncode == 0:
                return DeployResult(
                    service_name=",".join(args[-1:]) if args else "all",
                    status="deployed",
                    log=log,
                )
            else:
                return DeployResult(
                    service_name=",".join(args[-1:]) if args else "all",
                    status="failed",
                    log=log or f"docker compose exited with code {proc.returncode}",
                )

        except FileNotFoundError:
            return DeployResult(
                service_name="all",
                status="failed",
                log="Docker not found. Is Docker installed and on PATH?",
            )
        except asyncio.TimeoutError:
            return DeployResult(
                service_name="all",
                status="failed",
                log=f"docker compose timed out after {timeout}s",
            )
