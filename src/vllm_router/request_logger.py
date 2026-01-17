# Copyright 2024-2025 The vLLM Production Stack Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
import time
from datetime import datetime

from fastapi import Request

from vllm_router.log import init_logger

logger = init_logger(__name__)


class RequestLogger:
    """Request logger for recording routing decisions and performance data"""

    def __init__(self):
        """Initialize request logger"""
        self.log_enabled = False
        self.log_file = None
        self.requests_bodies_dir = None
        # Dictionary for temporarily storing request arrival times
        self.arrival_times = {}

    def enable_logging(self, enabled: bool = True, log_dir: str = None):
        """
        Enable or disable logging

        Args:
            enabled: Whether to enable logging
            log_dir: Log directory path, if provided write to file, otherwise save to project path
        """
        self.log_enabled = enabled
        self.log_file = None  # Reset log_file

        if enabled:
            # If log_dir is not specified, use project path
            if not log_dir:
                log_dir = os.getcwd()
                logger.info(
                    f"No log directory specified, will use current working directory: {log_dir}"
                )

            try:
                # Ensure log directory exists
                os.makedirs(log_dir, exist_ok=True)

                # Create request body storage directory
                self.requests_bodies_dir = os.path.join(log_dir, "request_bodies")
                os.makedirs(self.requests_bodies_dir, exist_ok=True)

                # Create log file name with timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_file = os.path.join(
                    log_dir, f"router_requests_logs_{timestamp}.csv"
                )

                # Create log file and write header
                with open(log_file, "w") as f:
                    f.write(
                        "timestamp,request_id,conversation_id,arrival_time,routing_method,target_engine,process_time\n"
                    )

                self.log_file = log_file
                logger.info(f"Request routing logs will be written to file: {log_file}")
            except Exception as e:
                logger.error(f"Failed to create log file: {str(e)}")
                self.log_file = None

            logger.info("Request routing logging has been enabled")

    def log_request_routed(
        self,
        arrival_time: float,
        request_id: str,
        routing_method: str,
        target_engine: str,
        session_id: str = None,
        process_time: float = None,
    ):
        """Record request routing decision and timestamp, and write to file (if enabled)"""
        if not self.log_enabled or not self.log_file:
            return

        # Ensure session_id has a value
        session_id = session_id or "unknown"

        # Write to file
        try:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log_line = f"{timestamp},{request_id},{session_id},{arrival_time},{routing_method},{target_engine},{process_time}\n"

            with open(self.log_file, "a") as f:
                f.write(log_line)

        except Exception as e:
            logger.error(f"Failed to write to log file: {str(e)}")

    def log_request_body(self, request_body, request_id=None):
        """
        Log request body to a separate file

        Args:
            request_body: Request body content
            request_id: Request ID, if None then try to extract from request body
        """
        if not self.log_enabled or not self.requests_bodies_dir:
            return

        if not request_id:
            # Try to extract request_id from request body
            try:
                body_json = json.loads(request_body)
                request_id = body_json.get("id", str(int(time.time())))
            except:
                request_id = str(int(time.time()))

        # Create file name
        file_path = os.path.join(self.requests_bodies_dir, f"{request_id}.json")

        # Write request body to file
        try:
            with open(file_path, "wb") as f:
                if isinstance(request_body, bytes):
                    f.write(request_body)
                else:
                    f.write(request_body.encode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to write request body file: {str(e)}")

    def clear_logs(self):
        """Clear temporarily stored arrival times"""
        self.arrival_times.clear()


# Create global request logger instance
request_logger = RequestLogger()
