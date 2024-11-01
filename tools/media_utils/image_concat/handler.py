import sys
sys.path.append("../..")

import os
from PIL import Image
import novadash_utils


async def image_concat(args: dict, _: str = None, env: str = None):
    image_urls = args.get("images")
    height = args.get("height")

    images = []
    for image_url in image_urls:
        image_filename = image_url.split("/")[-1]
        image = novadash_utils.download_file(image_url, image_filename)
        image = Image.open(image)
        width = int(height * image.size[0] / image.size[1])
        image = image.resize((width, height), Image.Resampling.LANCZOS)
        images.append(image)

    # Calculate total width for the final image
    total_width = sum(img.size[0] for img in images)
    # Create new image with combined width and specified height
    combined_image = Image.new('RGB', (total_width, height))
    
    # Paste images horizontally
    x_offset = 0
    for img in images:
        combined_image.paste(img, (x_offset, 0))
        x_offset += img.size[0]

    # Save combined image
    result_filename = f"combined_{height}px.png"
    combined_image.save(result_filename)

    return [result_filename]
