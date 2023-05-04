#  Copyright 2023 Red Hat, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import logging
import os
import uuid

from django.conf import settings
from podman import PodmanClient
from podman.domain.containers import Container
from podman.domain.images import Image
from podman.errors import ContainerError, ImageNotFound
from podman.errors.exceptions import APIError

from aap_eda.core import models

from .activation_db_logger import ActivationDbLogger
from .exceptions import ActivationException

logger = logging.getLogger(__name__)

FLUSH_AT_END = -1


class ActivationPodman:
    def __init__(
        self,
        decision_environment: models.DecisionEnvironment,
        podman_url: str,
        activation_db_logger: ActivationDbLogger,
    ) -> None:
        self.decision_environment = decision_environment
        self.activation_db_logger = activation_db_logger

        if not self.decision_environment.image_url:
            raise ActivationException(
                f"DecisionEnvironment: {self.decision_environment.name} does"
                " not have image_url set"
            )

        if podman_url:
            self.podman_url = podman_url
        else:
            self._default_podman_url()
        logger.info(f"Using podman socket: {self.podman_url}")

        self.client = PodmanClient(base_url=self.podman_url)
        self._login()

        self.image = self._pull_image()
        logger.info(self.client.version())

    def run_worker_mode(
        self,
        ws_url: str,
        ws_ssl_verify: str,
        activation_instance_id: str,
        heartbeat: str,
        ports: dict,
    ) -> Container:
        try:
            """Run ansible-rulebook in worker mode."""
            args = [
                "ansible-rulebook",
                "--worker",
                "--websocket-ssl-verify",
                ws_ssl_verify,
                "--websocket-address",
                ws_url,
                "--id",
                str(activation_instance_id),
                "--heartbeat",
                str(heartbeat),
                settings.ANSIBLE_RULEBOOK_LOG_LEVEL,
            ]

            container = self.client.containers.run(
                image=self.decision_environment.image_url,
                command=args,
                stdout=True,
                stderr=True,
                remove=True,
                detach=True,
                name=f"eda-{activation_instance_id}-{uuid.uuid4()}",
                ports=ports,
            )

            logger.info(
                f"Created container: "
                f"name: {container.name}, "
                f"ports: {container.ports}, "
                f"status: {container.status}, "
                f"command: {args}"
            )

            self._save_logs(
                container=container,
                activation_instance_id=activation_instance_id,
            )

            return container
        except ContainerError:
            logger.exception("Container error")
            raise
        except ImageNotFound:
            logger.exception("Image not found")
            raise
        except APIError:
            logger.exception("Container run failed")
            raise

    def _default_podman_url(self) -> None:
        if os.getuid() == 0:
            self.podman_url = "unix:///run/podman/podman.sock"
        else:
            xdg_runtime_dir = os.getenv(
                "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
            )
            self.podman_url = f"unix://{xdg_runtime_dir}/podman/podman.sock"

    def _login(self) -> None:
        credential = self.decision_environment.credential
        if not credential:
            return

        try:
            registry = self.decision_environment.image_url.split("/")[0]
            self.client.login(
                username=credential.username,
                password=credential.secret.get_secret_value(),
                registry=registry,
            )

            logger.debug(
                f"{credential.username} login succeeded to {registry}"
            )
        except APIError:
            logger.exception("Login failed")
            raise

    def _pull_image(self) -> Image:
        credential = self.decision_environment.credential
        try:
            kwargs = {}
            if credential:
                kwargs["auth_config"] = {
                    "username": credential.username,
                    "password": credential.secret.get_secret_value(),
                }
            return self.client.images.pull(
                self.decision_environment.image_url, **kwargs
            )
        except ImageNotFound:
            logger.exception(
                f"Image {self.decision_environment.image_url} not found"
            )
            raise

    def _save_logs(
        self, container: Container, activation_instance_id: str
    ) -> None:
        dkg = container.logs(
            stream=True, follow=True, stderr=True, stdout=True
        )
        try:
            while True:
                line = next(dkg).decode("utf-8")
                self.activation_db_logger.write(line)
        except StopIteration:
            logger.info(f"log stream ended for {container.name}")

        self.return_code = container.wait(condition="exited")
        logger.info(f"return_code: {self.return_code}")

        self.activation_db_logger.write(
            f"Container exit code: {self.return_code}"
        )
        logger.info(
            f"{self.activation_db_logger.lines_written()}"
            " activation instance log entries created."
        )

        if self.return_code > 0:
            raise ActivationException(f"Activation failed in {container.name}")