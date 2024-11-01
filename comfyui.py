from urllib.error import URLError
from datetime import datetime
from bson import ObjectId
import os
import re
import git
import time
import json
import glob
import modal
import shutil
import urllib
import tarfile
import pathlib
import tempfile
import subprocess
import copy

from models import Task, User
from mongo import mongo_client, get_collection
import tool
import novadash_utils

GPUs = {
    "A100": modal.gpu.A100(),
    "A100-80GB": modal.gpu.A100(size="80GB")
}

if not os.getenv("WORKSPACE"):
    raise Exception("No workspace selected")

workspace_name = os.getenv("WORKSPACE")
app_name = f"comfyui-{workspace_name}"
test_workflows = os.getenv("WORKFLOWS")
root_workflows_folder = "private_workflows" if os.getenv("PRIVATE") else "workflows"
test_all = True if os.getenv("TEST_ALL") else False

def install_comfyui():
    snapshot = json.load(open("/root/workspace/snapshot.json", 'r'))
    comfyui_commit_sha = snapshot["comfyui"]
    subprocess.run(["git", "init", "."], check=True)
    subprocess.run(["git", "remote", "add", "--fetch", "origin", "https://github.com/comfyanonymous/ComfyUI"], check=True)
    subprocess.run(["git", "checkout", comfyui_commit_sha], check=True)
    subprocess.run(["pip", "install", "xformers!=0.0.18", "-r", "requirements.txt", "--extra-index-url", "https://download.pytorch.org/whl/cu121"], check=True)


def install_custom_nodes():
    snapshot = json.load(open("/root/workspace/snapshot.json", 'r'))
    custom_nodes = snapshot["git_custom_nodes"]
    for url, node in custom_nodes.items():
        print(f"Installing custom node {url} with hash {hash}")
        install_custom_node_with_retries(url, node['hash'])
    post_install_commands = snapshot.get("post_install_commands", [])
    for cmd in post_install_commands:
        os.system(cmd)


def install_custom_node_with_retries(url, hash, max_retries=3): 
    for attempt in range(max_retries + 1):
        try:
            install_custom_node(url, hash)
            return
        except Exception as e:
            if attempt < max_retries:
                print(f"Attempt {attempt + 1} failed because: {e}. Retrying...")
                time.sleep(5)
            else:
                print(f"All attempts failed. Error: {e}")
                raise

def install_custom_node(url, hash):
    repo_name = url.split("/")[-1].split(".")[0]
    repo_path = f"custom_nodes/{repo_name}"
    if os.path.exists(repo_path):
        return
    repo = git.Repo.clone_from(url, repo_path)
    repo.submodule_update(recursive=True)    
    repo.git.checkout(hash)
    for root, _, files in os.walk(repo_path):
        for file in files:
            if file.startswith("requirements") and file.endswith((".txt", ".pip")):
                try:
                    requirements_path = os.path.join(root, file)
                    if "with-cupy" in requirements_path: # hack for ComfyUI-Frame-Interpolation, don't use CuPy
                        continue
                    subprocess.run(["pip", "install", "-r", requirements_path], check=True)
                except Exception as e:
                    print(f"Error installing requirements: {e}")

def download_files():
    downloads = json.load(open("/root/workspace/downloads.json", 'r'))
    for path, url in downloads.items():
        comfy_path = pathlib.Path("/root") / path
        vol_path = pathlib.Path("/data") / path
        if vol_path.is_file():
            print(f"Skipping download, getting {path} from cache")
        else:
            print(f"Downloading {url} to {vol_path}")
            vol_path.parent.mkdir(parents=True, exist_ok=True)
            novadash_utils.download_file(url, vol_path)
            downloads_vol.commit()
        try:
            comfy_path.parent.mkdir(parents=True, exist_ok=True)
            comfy_path.symlink_to(vol_path)
        except Exception as e:
            raise Exception(f"Error linking {comfy_path} to {vol_path}: {e}")
        if not pathlib.Path(comfy_path).exists():
            raise Exception(f"No file found at {comfy_path}")


image = (
    modal.Image.debian_slim(python_version="3.11")
    .env({"COMFYUI_PATH": "/root", "COMFYUI_MODEL_PATH": "/root/models"}) 
    .env({"TEST_ALL": os.getenv("TEST_ALL")})
    .apt_install("git", "git-lfs", "libgl1-mesa-glx", "libglib2.0-0", "libmagic1", "ffmpeg")
    .pip_install(
        "httpx", "tqdm", "websocket-client", "gitpython", "boto3", "omegaconf",
        "requests", "Pillow", "fastapi==0.103.1", "python-magic", "replicate", 
        "python-dotenv", "pyyaml", "instructor==1.2.6", "torch==2.3.1", "torchvision", "packaging",
        "torchaudio", "pydub", "moviepy", "accelerate", "pymongo", "google-cloud-aiplatform")
    .env({"WORKSPACE": workspace_name}) 
    .copy_local_file(f"../{root_workflows_folder}/workspaces/{workspace_name}/snapshot.json", "/root/workspace/snapshot.json")
    .copy_local_file(f"../{root_workflows_folder}/workspaces/{workspace_name}/downloads.json", "/root/workspace/downloads.json")
    .run_function(install_comfyui, force_build=True)
    .run_function(install_custom_nodes, gpu=modal.gpu.A100())
    .copy_local_dir(f"../{root_workflows_folder}/workspaces/{workspace_name}", "/root/workspace")
    .env({"WORKFLOWS": test_workflows})
)

gpu = modal.gpu.A100()

downloads_vol = modal.Volume.from_name(
    "comfy-downloads", 
    create_if_missing=True
)

app = modal.App(
    name=app_name, 
    secrets=[
        modal.Secret.from_name("s3-credentials"),
        modal.Secret.from_name("mongo-credentials"),
        modal.Secret.from_name("openai"),
        modal.Secret.from_name("gcp-credentials"),
    ]
)

@app.cls(
    image=image,
    gpu=gpu,
    cpu=8.0,
    volumes={"/data": downloads_vol},
    concurrency_limit=3,
    container_idle_timeout=60,
    timeout=3600,
)
class ComfyUI:
    
    def _start(self, port=8188):
        print("Start sertver")
        t1 = time.time()
        self.server_address = f"127.0.0.1:{port}"
        cmd = f"python main.py --dont-print-server --listen --port {port}"
        subprocess.Popen(cmd, shell=True)
        while not self._is_server_running():
            time.sleep(1)
        t2 = time.time()
        self.launch_time = t2 - t1

    def _execute(self, workflow_name: str, args: dict, env: str):
        print("args", workflow_name, args)
        tool_path = f"/root/workspace/workflows/{workflow_name}"
        tool_ = tool.load_tool(tool_path)
        workflow = json.load(open(f"{tool_path}/workflow_api.json", 'r'))
        self._validate_comfyui_args(workflow, tool_)
        workflow = self._inject_args_into_workflow(workflow, tool_, args, env=env)
        prompt_id = self._queue_prompt(workflow)['prompt_id']
        outputs = self._get_outputs(prompt_id)
        print("comfyui outputs", outputs)
        output = outputs[str(tool_.comfyui_output_node_id)]
        intermediate_outputs = {
            io.name: outputs[str(io.node_id)]
            for io in tool_.comfyui_intermediate_outputs or []
        }
        print("outputs ", output)
        if not output:
            raise Exception(f"No output found for {workflow_name} at output node {tool_.comfyui_output_node_id}") 
        return output, intermediate_outputs

    @modal.method()
    def run(self, workflow_name: str, args: dict, env: str = "STAGE"):
        output, intermediate_outputs = self._execute(workflow_name, args, env=env)
        print("intermediate outputs", intermediate_outputs)
        result = novadash_utils.upload_media(output, env=env, save_thumbnails=False)
        result[0]["intermediateOutputs"] = {
            k: novadash_utils.upload_media(v, env=env, save_thumbnails=False)
            for k, v in intermediate_outputs.items()
        }
        return result

    @modal.method()
    def run_task(self, task_id: str, env: str):
        task = Task.from_id(document_id=task_id, env=env)
        start_time = datetime.utcnow()
        queue_time = (start_time - task.createdAt).total_seconds()
        boot_time = queue_time - self.launch_time if self.launch_time else 0

        task.update({
            "status": "running",
            "performance": {"waitTime": queue_time, "bootTime": boot_time}
        })
        task_update = {}

        result = []
        n_samples = task.args.get("n_samples", 1)
        print("======\nargs", task.workflow, task.args)

        try:            
            for i in range(n_samples):
                args = task.args.copy()
                if "seed" in args:
                    args["seed"] = args["seed"] + i

                workflow = task.parent_tool or task.workflow
                output, intermediate_outputs = self._execute(workflow, args, env=env)
                print("intermediate_outputs", intermediate_outputs)

                result_ = novadash_utils.upload_media(output, env=env)
                if intermediate_outputs:
                    result_[0]["intermediateOutputs"] = {
                        k: novadash_utils.upload_media(v, env=env, save_thumbnails=False)
                        for k, v in intermediate_outputs.items()
                    }
                
                result.extend(result_)

                if i == n_samples - 1:
                    task_update = {
                        "status": "completed", 
                        "result": result
                    }
                else:
                    task_update = {
                        "status": "running", 
                        "result": result
                    }
                    task.update(task_update)
    
            return task_update
        
        except Exception as e:
            print("Error", e)   
            task_update = {"status": "failed", "error": str(e)}
            refund_amount = (task.cost or 0) * (n_samples - len(result)) / n_samples
            user = User.from_id(task.user, env=env)
            user.refund_manna(refund_amount)
            raise e
        
        finally:
            run_time = datetime.utcnow() - start_time
            task_update["performance.runTime"] = run_time.total_seconds()
            task.update(task_update)
            self.launch_time = 0
            print("task_update", task_update)

    @modal.enter()
    def enter(self):
        self._start()

    @modal.build()
    def downloads(self):
        download_files()
            
    @modal.build()
    def test_workflows(self):
        print(" ==== TESTING WORKFLOWS ====")
        t1 = time.time()
        self._start()
        t2 = time.time()
        
        results = {"_performance": {"launch": t2 - t1}}
        workflows_dir = pathlib.Path("/root/workspace/workflows")
        workflow_names = [f.name for f in workflows_dir.iterdir() if f.is_dir()]
        test_workflows = os.getenv("WORKFLOWS")
        if test_workflows:
            test_workflows = test_workflows.split(",")
            if not all([w in workflow_names for w in test_workflows]):
                raise Exception(f"One or more invalid workflows found: {', '.join(test_workflows)}")
            workflow_names = test_workflows

        if not workflow_names:
            raise Exception("No workflows found!")

        for workflow in workflow_names:
            test_all = os.getenv("TEST_ALL", False)
            if test_all:
                tests = glob.glob(f"/root/workspace/workflows/{workflow}/test*.json")
            else:
                tests = [f"/root/workspace/workflows/{workflow}/test.json"]
            print("Running tests: ", tests)
            for test in tests:
                tool_ = tool.load_tool(f"/root/workspace/workflows/{workflow}")
                test_args = json.loads(open(test, "r").read())
                test_args = tool_.prepare_args(test_args)
                test_name = f"{workflow}_{os.path.basename(test)}"
                print(f"Running test: {test_name}")
                t1 = time.time()
                output, intermediate_outputs = self._execute(workflow, test_args, env="STAGE")
                if not output:
                    raise Exception(f"No output from {test_name}")
                result = novadash_utils.upload_media(output, env="STAGE")
                if intermediate_outputs:
                    result[0]["intermediateOutputs"] = {
                        k: novadash_utils.upload_media(v, env="STAGE", save_thumbnails=False)
                        for k, v in intermediate_outputs.items()
                    }
                t2 = time.time()                
                results[test_name] = result
                results["_performance"][test_name] = t2 - t1

        with open("_test_results_.json", "w") as f:
            json.dump(results, f, indent=4)

    @modal.method()
    def print_test_results(self):
        with open("_test_results_.json", "r") as f:
            results = json.load(f)
        print("\n\n\n============ Test Results ============")
        print(json.dumps(results, indent=4))

    def _is_server_running(self):
        try:
            url = f"http://{self.server_address}/history/123"
            with urllib.request.urlopen(url) as response:
                return response.status == 200
        except URLError:
            return False

    def _queue_prompt(self, prompt):
        data = json.dumps({"prompt": prompt}).encode('utf-8')
        req = urllib.request.Request("http://{}/prompt".format(self.server_address), data=data)
        return json.loads(urllib.request.urlopen(req).read())

    def _get_history(self, prompt_id):
        with urllib.request.urlopen("http://{}/history/{}".format(self.server_address, prompt_id)) as response:
            return json.loads(response.read())

    def _get_outputs(self, prompt_id):        
        while True:
            outputs = {}
            history = self._get_history(prompt_id)
            if prompt_id not in history:
                time.sleep(1)
                continue
            history = history[prompt_id]                        
            status = history["status"]
            status_str = status.get("status_str")
            if status_str == "error":
                messages = status.get("messages")
                errors = [                    
                    f"ComfyUI Error: {v.get('node_type')} {v.get('exception_type')}, {v.get('exception_message')}"
                    for k, v in messages if k == "execution_error"
                ]
                error_str = ", ".join(errors)
                print("error", error_str)
                raise Exception(error_str)

            for _ in history['outputs']:
                for node_id in history['outputs']:
                    node_output = history['outputs'][node_id]
                    if 'images' in node_output:
                        outputs[node_id] = [
                            os.path.join("output", image['subfolder'], image['filename'])
                            for image in node_output['images']
                        ]
                    elif 'gifs' in node_output:
                        outputs[node_id] = [
                            os.path.join("output", video['subfolder'], video['filename'])
                            for video in node_output['gifs']
                        ]
                    elif 'audio' in node_output:
                        outputs[node_id] = [
                            os.path.join("output", audio['subfolder'], audio['filename'])
                            for audio in node_output['audio']
                        ]
            
            print("comfy outputs", outputs)
            if not outputs:
                raise Exception("No outputs found")
            
            return outputs

    def _inject_embedding_mentions_sdxl(self, text, embedding_trigger, embeddings_filename, lora_mode, lora_strength):
        # Hardcoded computation of the token_strength for the embedding trigger:
        token_strength = 0.5 + lora_strength / 2

        reference = f'(embedding:{embeddings_filename})'
        
        # Make two deep copies of the input text:
        user_prompt = copy.deepcopy(text)
        lora_prompt = copy.deepcopy(text)

        if lora_mode == "face" or lora_mode == "object" or lora_mode == "concept":
            # Match all variations of the embedding_trigger:
            pattern = r'(<{0}>|<{1}>|{0}|{1})'.format(
                re.escape(embedding_trigger),
                re.escape(embedding_trigger.lower())
            )
            lora_prompt = re.sub(pattern, reference, lora_prompt, flags=re.IGNORECASE)
            lora_prompt = re.sub(r'(<concept>)', reference, lora_prompt, flags=re.IGNORECASE)
            if lora_mode == "face":
                base_word = "person"
            else:
                base_word = "object"

            user_prompt = re.sub(pattern, base_word, user_prompt, flags=re.IGNORECASE)
            user_prompt = re.sub(r'(<concept>)', base_word, user_prompt, flags=re.IGNORECASE)

        if reference not in lora_prompt: # Make sure the concept is always triggered:
            if lora_mode == "style":
                lora_prompt = f"in the style of {reference}, {lora_prompt}"
            else:
                lora_prompt = f"{reference}, {lora_prompt}"

        return user_prompt, lora_prompt

    
    def _inject_embedding_mentions_flux(self, text, embedding_trigger, caption_prefix):
        pattern = r'(<{0}>|<{1}>|{0}|{1})'.format(
            re.escape(embedding_trigger),
            re.escape(embedding_trigger.lower())
        )
        text = re.sub(pattern, caption_prefix, text, flags=re.IGNORECASE)
        text = re.sub(r'(<concept>)', caption_prefix, text, flags=re.IGNORECASE)

        if caption_prefix not in text: # Make sure the concept is always triggered:
            text = f"{caption_prefix}, {text}"

        return text
    

    def _transport_lora_flux(self, lora_url: str):
        loras_folder = "/root/models/loras"

        print("tl download lora", lora_url)
        if not re.match(r'^https?://', lora_url):
            raise ValueError(f"Lora URL Invalid: {lora_url}")
        
        lora_filename = lora_url.split("/")[-1]    
        lora_path = os.path.join(loras_folder, lora_filename)
        print("tl destination folder", loras_folder)

        if os.path.exists(lora_path):
            print("Lora safetensors file already extracted. Skipping.")
        else:
            novadash_utils.download_file(lora_url, lora_path)
            if not os.path.exists(lora_path):
                raise FileNotFoundError(f"The LoRA tar file {lora_path} does not exist.")
        
        print("destination path", lora_path)
        print("lora filename", lora_filename)

        return lora_filename

    def _transport_lora_sdxl(self, lora_url: str):
        downloads_folder = "/root/downloads"
        loras_folder = "/root/models/loras"
        embeddings_folder = "/root/models/embeddings"

        print("tl download lora", lora_url)
        if not re.match(r'^https?://', lora_url):
            raise ValueError(f"Lora URL Invalid: {lora_url}")
        
        lora_filename = lora_url.split("/")[-1]    
        name = lora_filename.split(".")[0]
        destination_folder = os.path.join(downloads_folder, name)
        print("tl destination folder", destination_folder)

        if os.path.exists(destination_folder):
            print("Lora bundle already extracted. Skipping.")
        else:
            try:
                lora_tarfile = novadash_utils.download_file(lora_url, f"/root/downloads/{lora_filename}")
                if not os.path.exists(lora_tarfile):
                    raise FileNotFoundError(f"The LoRA tar file {lora_tarfile} does not exist.")
                with tarfile.open(lora_tarfile, "r:*") as tar:
                    tar.extractall(path=destination_folder)
                    print("Extraction complete.")
            except Exception as e:
                raise IOError(f"Failed to extract tar file: {e}")

        extracted_files = os.listdir(destination_folder)
        print("tl, extracted files", extracted_files)

        # Find lora and embeddings files using regex
        lora_pattern = re.compile(r'.*_lora\.safetensors$')
        embeddings_pattern = re.compile(r'.*_embeddings\.safetensors$')

        lora_filename = next((f for f in extracted_files if lora_pattern.match(f)), None)
        embeddings_filename = next((f for f in extracted_files if embeddings_pattern.match(f)), None)
        training_args_filename = next((f for f in extracted_files if f == "training_args.json"), None)

        if training_args_filename:
            with open(os.path.join(destination_folder, training_args_filename), "r") as f:
                training_args = json.load(f)
                lora_mode = training_args["concept_mode"]
                embedding_trigger = training_args["name"]
        else:
            lora_mode = None
            embedding_trigger = embeddings_filename.split('_embeddings.safetensors')[0]

        # hack to correct for older lora naming convention
        if not lora_filename:
            print("Lora file not found with standard naming convention. Searching for alternative...")
            lora_filename = next((f for f in extracted_files if f.endswith('.safetensors') and 'embedding' not in f.lower()), None)
            if not lora_filename:
                raise FileNotFoundError(f"Unable to find a lora *.safetensors file in {extracted_files}")
            
        print("tl, lora mode:", lora_mode)
        print("tl, lora filename:", lora_filename)
        print("tl, embeddings filename:", embeddings_filename)
        print("tl, embedding_trigger:", embedding_trigger)

        for file in [lora_filename, embeddings_filename]:
            if str(file) not in extracted_files:
                raise FileNotFoundError(f"Required file {file} does not exist in the extracted files: {extracted_files}")

        if not os.path.exists(loras_folder):
            os.makedirs(loras_folder)
        if not os.path.exists(embeddings_folder):
            os.makedirs(embeddings_folder)

        # copy lora file to loras folder
        lora_path = os.path.join(destination_folder, lora_filename)
        lora_copy_path = os.path.join(loras_folder, lora_filename)
        shutil.copy(lora_path, lora_copy_path)
        print(f"LoRA {lora_path} has been moved to {lora_copy_path}")

        # copy embedding file to embeddings folder
        embeddings_path = os.path.join(destination_folder, embeddings_filename)
        embeddings_copy_path = os.path.join(embeddings_folder, embeddings_filename)
        shutil.copy(embeddings_path, embeddings_copy_path)
        print(f"Embeddings {embeddings_path} has been moved to {embeddings_copy_path}")
        
        return lora_filename, embeddings_filename, embedding_trigger, lora_mode

    def _url_to_filename(self, url):
        filename = url.split('/')[-1]
        filename = re.sub(r'\?.*$', '', filename)
        max_length = 255
        if len(filename) > max_length: # ensure filename is not too long
            name, ext = os.path.splitext(filename)
            filename = name[:max_length - len(ext)] + ext
        return filename    

    def _validate_comfyui_args(self, workflow, tool_):
        comfyui_map = {
            param.name: param 
            for param in tool_.parameters if param.comfyui
        }
        for _, param in comfyui_map.items():
            comfyui = param.comfyui
            node_id, field, subfield, remap = str(comfyui.node_id), comfyui.field, comfyui.subfield, comfyui.remap
            subfields = [s.strip() for s in subfield.split(",")]
            for subfield in subfields:
                if node_id not in workflow or field not in workflow[node_id] or subfield not in workflow[node_id][field]:
                    raise Exception(f"Node ID {node_id}, field {field}, subfield {subfield} not found in workflow")
            for r in remap or []:
                subfields = [s.strip() for s in r.subfield.split(",")]
                for subfield in subfields:
                    if str(r.node_id) not in workflow or r.field not in workflow[str(r.node_id)] or subfield not in workflow[str(r.node_id)][r.field]:
                        raise Exception(f"Node ID {r.node_id}, field {r.field}, subfield {subfield} not found in workflow")
                values = {v.input: v.output for v in r.value}
                if not param.choices:
                    raise Exception(f"Remap parameter {param.name} has no original choices")
                if not all(choice in param.choices for choice in values.keys()):
                    raise Exception(f"Remap parameter {param.name} has invalid choices: {values}")
                if not all(choice in values.keys() for choice in param.choices):
                    raise Exception(f"Remap parameter {param.name} is missing original choices: {param.choices}")

    def _inject_args_into_workflow(self, workflow, tool_, args, env="STAGE"):
        embedding_trigger = None
        caption_prefix = None

        # Helper function to validate and normalize URLs
        def validate_url(url):
            if not url:
                return None
            if not isinstance(url, str):
                raise ValueError(f"Invalid URL type: {type(url)}. Expected string.")
            # Add protocol if missing
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            return url
    
        print("args:", args)
        
        # download and transport files
        for param in tool_.parameters: 
            if param.type in tool.FILE_TYPES:
                url = args.get(param.name)
                args[param.name] = novadash_utils.download_file(url, f"/root/input/{self._url_to_filename(url)}") if url else None
            
            elif param.type in tool.FILE_ARRAY_TYPES:
                urls = args.get(param.name)
                if urls:
                    try:
                        args[param.name] = [
                            novadash_utils.download_file(
                                validate_url(url),
                                f"/root/input/{self._url_to_filename(validate_url(url))}"
                            ) if url else None
                            for url in urls
                        ]
                    except Exception as e:
                        raise Exception(f"Error processing {param.name} array: {str(e)}")
                else:
                    print("WARNING: No valid URLs found for {param.name}")
                    args[param.name] = None
                
            elif param.type == tool.ParameterType.LORA:
                lora_id = args.get(param.name)
                print("LORA ID", lora_id)
                if not lora_id:
                    args[param.name] = None
                    args["lora_strength"] = 0
                    print("REMOVE LORA")
                    continue
                
                print("get models", env)
                models = get_collection("models", env=env)
                lora = models.find_one({"_id": ObjectId(lora_id)})

                # very nasty hack until we have a better way to transfer env to handlers
                print("lora", lora)
                if not lora:
                    print("lora not found, try prod")
                    models = get_collection("models", env="PROD")
                    lora = models.find_one({"_id": ObjectId(lora_id)})
                
                print("LORA", lora)
                base_model = lora.get("base_model")
                print("base model", base_model)
                if not lora:
                    raise Exception(f"Lora {lora_id} not found")

                lora_url = lora.get("checkpoint")
                #lora_name = lora.get("name")
                #pretrained_model = lora.get("args").get("sd_model_version")

                if not lora_url:
                    raise Exception(f"Lora {lora_id} has no checkpoint")
                else:
                    print("LORA URL", lora_url)

                print("base model", base_model)

                if base_model == "sdxl":
                    lora_filename, embeddings_filename, embedding_trigger, lora_mode = self._transport_lora_sdxl(lora_url)
                elif base_model == "flux-dev":
                    lora_filename = self._transport_lora_flux(lora_url)
                    embedding_trigger = lora.get("args", {}).get("name")
                    caption_prefix = lora.get("args", {}).get("caption_prefix")

                args[param.name] = lora_filename
                print("lora filename", lora_filename)
        
        # inject args
        comfyui_map = {
            param.name: param.comfyui 
            for param in tool_.parameters if param.comfyui
        }

        for key, comfyui in comfyui_map.items():
            value = args.get(key)
            if value is None or key == "no_token_prompt":
                continue

            # if there's a lora, replace mentions with embedding name
            if key == "prompt" and embedding_trigger:
                lora_strength = args.get("lora_strength", 0.5)
                if base_model == "flux-dev":
                    value = self._inject_embedding_mentions_flux(value, embedding_trigger, caption_prefix)
                elif base_model == "sdxl":  
                    no_token_prompt, value = self._inject_embedding_mentions_sdxl(value, embedding_trigger, embeddings_filename, lora_mode, lora_strength)
                    
                    if "no_token_prompt" in args:
                        no_token_mapping = next((param.comfyui for param in tool_.parameters if param.name == "no_token_prompt"), None)
                        if no_token_mapping:
                            print("Updating no_token_prompt for SDXL: ", no_token_prompt)
                            workflow[str(no_token_mapping.node_id)][no_token_mapping.field][no_token_mapping.subfield] = no_token_prompt

                print("prompt updated:", value)

            if comfyui.preprocessing is not None:
                if comfyui.preprocessing == "csv":
                    value = ",".join(value)

                elif comfyui.preprocessing == "concat":
                    value = ";\n".join(value)

                elif comfyui.preprocessing == "folder":
                    temp_subfolder = tempfile.mkdtemp(dir="/root/input")
                    if isinstance(value, list):
                        for i, file in enumerate(value):
                            filename = f"{i:06d}_{os.path.basename(file)}"
                            new_path = os.path.join(temp_subfolder, filename)
                            shutil.copy(file, new_path)
                    else:
                        shutil.copy(value, temp_subfolder)
                    value = temp_subfolder

            print("comfyui mapping")
            print(comfyui)

            node_id, field, subfield = str(comfyui.node_id), comfyui.field, comfyui.subfield
            subfields = [s.strip() for s in subfield.split(",")]
            for subfield in subfields:
                print("inject", node_id, field, subfield, " = ", value)
                workflow[node_id][field][subfield] = value  

            for r in comfyui.remap or []:
                subfields = [s.strip() for s in r.subfield.split(",")]
                for subfield in subfields:
                    values = {v.input: v.output for v in r.value}
                    output_value = values.get(value)
                    print("remap", str(r.node_id), r.field, subfield, " = ", output_value)
                    workflow[str(r.node_id)][r.field][subfield] = output_value

        return workflow


@app.local_entrypoint()
def run():
    comfyui = ComfyUI()
    comfyui.print_test_results.remote()
