from datetime import datetime
import argparse
import json
import random
import os
from tool import *
from mongo import get_collection

env = os.getenv("ENV", "STAGE")
if env not in ["PROD", "STAGE"]:
    raise Exception(f"Invalid environment: {env}. Must be PROD or STAGE")

# this controls order of tools in frontend
ordered_tools = [
    "txt2img", "flux_dev", "flux_schnell", 
    "layer_diffusion", "remix_flux_schnell", "remix", "inpaint", "flux_inpainting", "outpaint", "face_styler", 
    "upscaler", "background_removal", "background_removal_video", "style_transfer",
    "animate_3D", "txt2vid", "img2vid", "video_upscaler", "vid2vid_sdxl",
    "texture_flow", "runway",
    "stable_audio", "musicgen"
]


"""
TODO
- env: local (yaml), stage, prod
- get_tools(env=local, env=stage, env=prod)
- test_tools, test_api, test_sdk adapt
"""


def get_all_tools_from_yaml():
    tools = get_comfyui_tools("../workflows/workspaces")
    tools.update(get_comfyui_tools("../private_workflows/workspaces"))
    tools.update(get_tools("tools"))
    tools.update(get_tools("tools/media_utils"))
    return tools


def get_all_tools_from_mongo():
    tools_collection = get_collection("tools", env)
    tools = {}
    for tool in tools_collection.find():
        key = tool.pop("key")
        print("KEY", key)
        tool['cost_estimate'] = tool.pop('costEstimate')
        tool['output_type'] = tool.pop('outputType')
        tool['base_model'] = tool.pop('baseModel', None)
        if tool.get('parent_tool'):
            data = yaml.safe_load(open(f"tools/{key}/api.yaml", "r"))
            if data.get('cost_estimate'):
                data['cost_estimate'] = str(data['cost_estimate'])
            workspace = data.pop('parent_tool')
            data['workspace'] = workspace
            tools[key] = PresetTool(data, key=key, parent_tool_path=workspace)
        elif tool["handler"] == "comfyui":
            tools[key] = ComfyUITool(tool, key)
        elif tool["handler"] == "replicate":
            tools[key] = ReplicateTool(tool, key)
        elif tool["handler"] == "gcp":
            tools[key] = GCPTool(tool, key)
        else:
            tools[key] = ModalTool(tool, key)

    return tools


# available_tools = get_all_tools()
# if env == "PROD":
#     available_tools = {k: v for k, v in available_tools.items() if k in ordered_tools}


def update_tools():
    parser = argparse.ArgumentParser(description="Upload arguments")
    parser.add_argument('--env', choices=['STAGE', 'PROD'], default='STAGE', help='Environment to run in (STAGE or PROD)')
    parser.add_argument('--tools', nargs='+', help='List of tools to update')
    args = parser.parse_args()

    available_tools = get_all_tools_from_yaml()

    print(available_tools.keys())

    if args.tools:
        available_tools = {k: v for k, v in available_tools.items() if k in args.tools}

    tools_collection = get_collection("tools", args.env)
    api_tools_order = {tool: index for index, tool in enumerate(ordered_tools)}
    sorted_tools = sorted(available_tools.items(), 
                          key=lambda x: api_tools_order.get(x[0], len(ordered_tools)))
    
    for index, (tool_key, tool) in enumerate(sorted_tools):
        tool_config = tool.model_dump()


        # temporary until visible activated
        tool_config['private'] = not tool_config.pop('visible', True)


        tool_config['costEstimate'] = tool_config.pop('cost_estimate')
        tool_config['outputType'] = tool_config.pop('output_type')
        if 'base_model' in tool_config:
            tool_config['baseModel'] = tool_config.pop('base_model')
        if 'parent_tool' in tool_config:
            tool_config.pop('parent_tool')
            tool_config['parent_tool'] = tool.parent_tool.model_dump()
        tool_config["updatedAt"] = datetime.utcnow()
        
        if not args.tools:
            tool_config['order'] = index  # set order based on the new sorting
        
        existing_doc = tools_collection.find_one({"key": tool_key})        
        update_operation = {
            "$set": tool_config,
            "$setOnInsert": {"createdAt": datetime.utcnow()},
            "$unset": {k: "" for k in (existing_doc or {}) if k not in tool_config and k != "createdAt" and k != "_id"}
        }        
        tools_collection.update_one(
            {"key": tool_key},
            update_operation,
            upsert=True
        )
        
        parameters = ", ".join([p["name"] for p in tool_config.pop("parameters")])
        print(f"\033[38;5;{random.randint(1, 255)}m")
        print(f"\n\nUpdated {args.env} {tool_key}\n============")
        tool_config.pop("updatedAt")
        print(json.dumps(tool_config, indent=2))
        print(f"Parameters: {parameters}")
    
    print(f"\033[97m \n\n\nUpdated {len(available_tools)} tools : {', '.join(available_tools.keys())}")
        

if __name__ == "__main__":
    update_tools()
