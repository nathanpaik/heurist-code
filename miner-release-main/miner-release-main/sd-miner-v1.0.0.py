import time
import torch
import sys
from itertools import cycle
from pathlib import Path
import os
import logging
from multiprocessing import Process, set_start_method
import signal
import threading
from dotenv import load_dotenv

from mining_core.base import BaseConfig, ModelUpdater
from mining_core.utils import (
    check_cuda, get_hardware_description,
    fetch_and_download_config_files, get_local_model_ids,
    post_request, log_response, submit_job_result, initialize_logging_and_args
)

class MinerConfig(BaseConfig):
    def __init__(self, config_file, cuda_device_id=0):
        super().__init__(config_file, cuda_device_id)
        load_dotenv()

        miner_ids = self._load_and_validate_miner_ids()
        self.miner_id = self._assign_miner_id(miner_ids, cuda_device_id)

    def _load_and_validate_miner_ids(self):
        miner_ids = [os.getenv(f'MINER_ID_{i}') for i in range(self.num_cuda_devices)]
        for i, miner_id in enumerate(miner_ids):
            if miner_id is None:
                print(f"ERROR: Miner ID for GPU {i} not found in .env. Exiting...")
                raise ValueError(f"Miner ID for GPU {i} not found in .env.")
            if not miner_id.startswith("0x"):
                print(f"WARNING: Miner ID {miner_id} for GPU {i} does not start with '0x'.")
        return miner_ids
    
    def _assign_miner_id(self, miner_ids, cuda_device_id):
        if self.num_cuda_devices > 1 and miner_ids[cuda_device_id]:
            return miner_ids[cuda_device_id]
        elif miner_ids[0]:
            return miner_ids[0]
        else:
            print("ERROR: miner_id not found in .env. Exiting...")
            raise ValueError("miner_id not found in .env.")

def load_config(filename='config.toml', cuda_device_id=0):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, filename)
    return MinerConfig(config_path, cuda_device_id)

def send_miner_request(config, model_ids, min_deadline, current_model_id):
    request_data = {
        "miner_id": config.miner_id,
        "model_ids": model_ids,
        "min_deadline": min_deadline,
        "current_model_id": current_model_id
    }
    if time.time() - config.last_heartbeat >= 60:
        request_data['hardware'] = 'NVIDIA GeForce RTX 3090'
        request_data['version'] = 'sd-v1.0.0'
        config.last_heartbeat = time.time()
        logging.debug(f"Heartbeat updated at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(config.last_heartbeat))} with hardware '{request_data['hardware']}' and version {config.version} for miner ID {config.miner_id}.")
    
    start_time = time.time()
    response = post_request(config.base_url + "/miner_request", request_data, config.miner_id)
    end_time = time.time()
    request_latency = end_time - start_time

    # Assuming response.text contains the full text response from the server
    warning_indicator = "Warning:"
    if warning_indicator in response.text:
        # Extract the warning message and use strip() to remove any trailing quotation marks
        warning_message = response.text.split(warning_indicator)[1].strip('"')
        print(f"WARNING: {warning_message}")


    response_data = log_response(response, config.miner_id)

    try:
        # Check if the response contains a valid job and print the friendly message
        if response_data and 'job_id' in response_data and 'model_id' in response_data:
            job_id = response_data['job_id']
            model_id = response_data['model_id']
            print(f"Processing Request ID: {job_id}. Model ID: {model_id}.")
    except Exception as e:
        logging.error(f"Failed to process response data: {e}")

    return response_data, request_latency

def main(cuda_device_id):
    # torch.cuda.set_device(cuda_device_id)
    config = load_config(cuda_device_id=cuda_device_id)
    
    # The parent process should have already downloaded the model files
    # Now we just need to load them into memory
    fetch_and_download_config_files(config)

    
    executed = False
    while True:
        try:
            current_model_id = next(iter(config.loaded_models)) if config.loaded_models else None
            model_ids = get_local_model_ids(config)
            if len(model_ids) == 0:
                logging.info("No models found. Exiting...")
                exit(0)
                
            job, request_latency = send_miner_request(config, model_ids, config.min_deadline, current_model_id)

            if job is not None:
                job_start_time = time.time()  # Timestamp when the job starts processing
                logging.info(f"Processing Request ID: {job['job_id']}. Model ID: {job['model_id']}.")
                submit_job_result(config, config.miner_id, job, job['temp_credentials'], job_start_time, request_latency)
                executed = True
            else:
                logging.info("No job received.")
                executed = False
        except Exception as e:
            logging.error(f"Error occurred: {e}")
            import traceback
            traceback.print_exc()
            
        if not executed:
            time.sleep(2)
            
if __name__ == "__main__":
    processes = []
    def signal_handler(signum, frame):
        for p in processes:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    set_start_method('spawn', force=True)
    
    config = load_config()
    config = initialize_logging_and_args(config, miner_id=config.miner_id)

    # if config.num_cuda_devices > torch.cuda.device_count():
    #     print("Number of CUDA devices specified in config is greater than available. Exiting...")
    #     sys.exit(1)
    # check_cuda()

    fetch_and_download_config_files(config)

    # Initialize and start model updater before processing tasks
    model_updater = ModelUpdater(config=config.__dict__)  # Assuming config.__dict__ provides necessary settings

    # Start the model updater in a separate thread
    updater_thread = threading.Thread(target=model_updater.start_scheduled_updates)
    updater_thread.start()
    
    # TODO: There appear to be 1 leaked semaphore objects to clean up at shutdown
    # Launch a separate process for each CUDA device
    try:
        for i in range(config.num_cuda_devices):
            p = Process(target=main, args=(i,))
            p.start()
            processes.append(p)

        for p in processes:
            p.join()

    except KeyboardInterrupt:
        print("Main process interrupted. Terminating child processes.")
        for p in processes:
            p.terminate()
            p.join()
