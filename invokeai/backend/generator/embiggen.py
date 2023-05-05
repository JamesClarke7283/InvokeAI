"""
invokeai.backend.generator.embiggen descends from .generator
and generates with .generator.img2img
"""

import numpy as np
import torch
from PIL import Image
from tqdm import trange

import invokeai.backend.util.logging as logger

from .base import Generator
from .img2img import Img2Img

class Embiggen(Generator):
    def __init__(self, model, precision):
        super().__init__(model, precision)
        self.init_latent = None

    # Replace generate because Embiggen doesn't need/use most of what it does normallly
    def generate(
        self,
        prompt,
        iterations=1,
        seed=None,
        image_callback=None,
        step_callback=None,
        **kwargs,
    ):
        make_image = self.get_make_image(prompt, step_callback=step_callback, **kwargs)
        results = []
        seed = seed if seed else self.new_seed()

        # Noise will be generated by the Img2Img generator when called
        for _ in trange(iterations, desc="Generating"):
            # make_image will call Img2Img which will do the equivalent of get_noise itself
            image = make_image()
            results.append([image, seed])
            if image_callback is not None:
                image_callback(image, seed, prompt_in=prompt)
            seed = self.new_seed()
        return results

    @torch.no_grad()
    def get_make_image(
        self,
        prompt,
        sampler,
        steps,
        cfg_scale,
        ddim_eta,
        conditioning,
        init_img,
        strength,
        width,
        height,
        embiggen,
        embiggen_tiles,
        step_callback=None,
        **kwargs,
    ):
        """
        Returns a function returning an image derived from the prompt and multi-stage twice-baked potato layering over the img2img on the initial image
        Return value depends on the seed at the time you call it
        """
        assert (
            not sampler.uses_inpainting_model()
        ), "--embiggen is not supported by inpainting models"

        # Construct embiggen arg array, and sanity check arguments
        if embiggen == None:  # embiggen can also be called with just embiggen_tiles
            embiggen = [1.0]  # If not specified, assume no scaling
        elif embiggen[0] < 0:
            embiggen[0] = 1.0
            logger.warning(
                "Embiggen scaling factor cannot be negative, fell back to the default of 1.0 !"
            )
        if len(embiggen) < 2:
            embiggen.append(0.75)
        elif embiggen[1] > 1.0 or embiggen[1] < 0:
            embiggen[1] = 0.75
            logger.warning(
                "Embiggen upscaling strength for ESRGAN must be between 0 and 1, fell back to the default of 0.75 !"
            )
        if len(embiggen) < 3:
            embiggen.append(0.25)
        elif embiggen[2] < 0:
            embiggen[2] = 0.25
            logger.warning(
                "Overlap size for Embiggen must be a positive ratio between 0 and 1 OR a number of pixels, fell back to the default of 0.25 !"
            )

        # Convert tiles from their user-freindly count-from-one to count-from-zero, because we need to do modulo math
        # and then sort them, because... people.
        if embiggen_tiles:
            embiggen_tiles = list(map(lambda n: n - 1, embiggen_tiles))
            embiggen_tiles.sort()

        if strength >= 0.5:
            logger.warning(
                f"Embiggen may produce mirror motifs if the strength (-f) is too high (currently {strength}). Try values between 0.35-0.45."
            )

        # Prep img2img generator, since we wrap over it
        gen_img2img = Img2Img(self.model, self.precision)

        # Open original init image (not a tensor) to manipulate
        initsuperimage = Image.open(init_img)

        with Image.open(init_img) as img:
            initsuperimage = img.convert("RGB")

        # Size of the target super init image in pixels
        initsuperwidth, initsuperheight = initsuperimage.size

        # Increase by scaling factor if not already resized, using ESRGAN as able
        if embiggen[0] != 1.0:
            initsuperwidth = round(initsuperwidth * embiggen[0])
            initsuperheight = round(initsuperheight * embiggen[0])
            if embiggen[1] > 0:  # No point in ESRGAN upscaling if strength is set zero
                from ..restoration.realesrgan import ESRGAN

                esrgan = ESRGAN()
                logger.info(
                    f"ESRGAN upscaling init image prior to cutting with Embiggen with strength {embiggen[1]}"
                )
                if embiggen[0] > 2:
                    initsuperimage = esrgan.process(
                        initsuperimage,
                        embiggen[1],  # upscale strength
                        self.seed,
                        4,  # upscale scale
                    )
                else:
                    initsuperimage = esrgan.process(
                        initsuperimage,
                        embiggen[1],  # upscale strength
                        self.seed,
                        2,  # upscale scale
                    )
            # We could keep recursively re-running ESRGAN for a requested embiggen[0] larger than 4x
            #   but from personal experiance it doesn't greatly improve anything after 4x
            # Resize to target scaling factor resolution
            initsuperimage = initsuperimage.resize(
                (initsuperwidth, initsuperheight), Image.Resampling.LANCZOS
            )

        # Use width and height as tile widths and height
        # Determine buffer size in pixels
        if embiggen[2] < 1:
            if embiggen[2] < 0:
                embiggen[2] = 0
            overlap_size_x = round(embiggen[2] * width)
            overlap_size_y = round(embiggen[2] * height)
        else:
            overlap_size_x = round(embiggen[2])
            overlap_size_y = round(embiggen[2])

        # With overall image width and height known, determine how many tiles we need
        def ceildiv(a, b):
            return -1 * (-a // b)

        # X and Y needs to be determined independantly (we may have savings on one based on the buffer pixel count)
        # (initsuperwidth - width) is the area remaining to the right that we need to layers tiles to fill
        # (width - overlap_size_x) is how much new we can fill with a single tile
        emb_tiles_x = 1
        emb_tiles_y = 1
        if (initsuperwidth - width) > 0:
            emb_tiles_x = ceildiv(initsuperwidth - width, width - overlap_size_x) + 1
        if (initsuperheight - height) > 0:
            emb_tiles_y = ceildiv(initsuperheight - height, height - overlap_size_y) + 1
        # Sanity
        assert (
            emb_tiles_x > 1 or emb_tiles_y > 1
        ), f"ERROR: Based on the requested dimensions of {initsuperwidth}x{initsuperheight} and tiles of {width}x{height} you don't need to Embiggen! Check your arguments."

        # Prep alpha layers --------------
        # https://stackoverflow.com/questions/69321734/how-to-create-different-transparency-like-gradient-with-python-pil
        # agradientL is Left-side transparent
        agradientL = (
            Image.linear_gradient("L").rotate(90).resize((overlap_size_x, height))
        )
        # agradientT is Top-side transparent
        agradientT = Image.linear_gradient("L").resize((width, overlap_size_y))
        # radial corner is the left-top corner, made full circle then cut to just the left-top quadrant
        agradientC = Image.new("L", (256, 256))
        for y in range(256):
            for x in range(256):
                # Find distance to lower right corner (numpy takes arrays)
                distanceToLR = np.sqrt([(255 - x) ** 2 + (255 - y) ** 2])[0]
                # Clamp values to max 255
                if distanceToLR > 255:
                    distanceToLR = 255
                # Place the pixel as invert of distance
                agradientC.putpixel((x, y), round(255 - distanceToLR))

        # Create alternative asymmetric diagonal corner to use on "tailing" intersections to prevent hard edges
        # Fits for a left-fading gradient on the bottom side and full opacity on the right side.
        agradientAsymC = Image.new("L", (256, 256))
        for y in range(256):
            for x in range(256):
                value = round(max(0, x - (255 - y)) * (255 / max(1, y)))
                # Clamp values
                value = max(0, value)
                value = min(255, value)
                agradientAsymC.putpixel((x, y), value)

        # Create alpha layers default fully white
        alphaLayerL = Image.new("L", (width, height), 255)
        alphaLayerT = Image.new("L", (width, height), 255)
        alphaLayerLTC = Image.new("L", (width, height), 255)
        # Paste gradients into alpha layers
        alphaLayerL.paste(agradientL, (0, 0))
        alphaLayerT.paste(agradientT, (0, 0))
        alphaLayerLTC.paste(agradientL, (0, 0))
        alphaLayerLTC.paste(agradientT, (0, 0))
        alphaLayerLTC.paste(agradientC.resize((overlap_size_x, overlap_size_y)), (0, 0))
        # make masks with an asymmetric upper-right corner so when the curved transparent corner of the next tile
        # to its right is placed it doesn't reveal a hard trailing semi-transparent edge in the overlapping space
        alphaLayerTaC = alphaLayerT.copy()
        alphaLayerTaC.paste(
            agradientAsymC.rotate(270).resize((overlap_size_x, overlap_size_y)),
            (width - overlap_size_x, 0),
        )
        alphaLayerLTaC = alphaLayerLTC.copy()
        alphaLayerLTaC.paste(
            agradientAsymC.rotate(270).resize((overlap_size_x, overlap_size_y)),
            (width - overlap_size_x, 0),
        )

        if embiggen_tiles:
            # Individual unconnected sides
            alphaLayerR = Image.new("L", (width, height), 255)
            alphaLayerR.paste(agradientL.rotate(180), (width - overlap_size_x, 0))
            alphaLayerB = Image.new("L", (width, height), 255)
            alphaLayerB.paste(agradientT.rotate(180), (0, height - overlap_size_y))
            alphaLayerTB = Image.new("L", (width, height), 255)
            alphaLayerTB.paste(agradientT, (0, 0))
            alphaLayerTB.paste(agradientT.rotate(180), (0, height - overlap_size_y))
            alphaLayerLR = Image.new("L", (width, height), 255)
            alphaLayerLR.paste(agradientL, (0, 0))
            alphaLayerLR.paste(agradientL.rotate(180), (width - overlap_size_x, 0))

            # Sides and corner Layers
            alphaLayerRBC = Image.new("L", (width, height), 255)
            alphaLayerRBC.paste(agradientL.rotate(180), (width - overlap_size_x, 0))
            alphaLayerRBC.paste(agradientT.rotate(180), (0, height - overlap_size_y))
            alphaLayerRBC.paste(
                agradientC.rotate(180).resize((overlap_size_x, overlap_size_y)),
                (width - overlap_size_x, height - overlap_size_y),
            )
            alphaLayerLBC = Image.new("L", (width, height), 255)
            alphaLayerLBC.paste(agradientL, (0, 0))
            alphaLayerLBC.paste(agradientT.rotate(180), (0, height - overlap_size_y))
            alphaLayerLBC.paste(
                agradientC.rotate(90).resize((overlap_size_x, overlap_size_y)),
                (0, height - overlap_size_y),
            )
            alphaLayerRTC = Image.new("L", (width, height), 255)
            alphaLayerRTC.paste(agradientL.rotate(180), (width - overlap_size_x, 0))
            alphaLayerRTC.paste(agradientT, (0, 0))
            alphaLayerRTC.paste(
                agradientC.rotate(270).resize((overlap_size_x, overlap_size_y)),
                (width - overlap_size_x, 0),
            )

            # All but X layers
            alphaLayerABT = Image.new("L", (width, height), 255)
            alphaLayerABT.paste(alphaLayerLBC, (0, 0))
            alphaLayerABT.paste(agradientL.rotate(180), (width - overlap_size_x, 0))
            alphaLayerABT.paste(
                agradientC.rotate(180).resize((overlap_size_x, overlap_size_y)),
                (width - overlap_size_x, height - overlap_size_y),
            )
            alphaLayerABL = Image.new("L", (width, height), 255)
            alphaLayerABL.paste(alphaLayerRTC, (0, 0))
            alphaLayerABL.paste(agradientT.rotate(180), (0, height - overlap_size_y))
            alphaLayerABL.paste(
                agradientC.rotate(180).resize((overlap_size_x, overlap_size_y)),
                (width - overlap_size_x, height - overlap_size_y),
            )
            alphaLayerABR = Image.new("L", (width, height), 255)
            alphaLayerABR.paste(alphaLayerLBC, (0, 0))
            alphaLayerABR.paste(agradientT, (0, 0))
            alphaLayerABR.paste(
                agradientC.resize((overlap_size_x, overlap_size_y)), (0, 0)
            )
            alphaLayerABB = Image.new("L", (width, height), 255)
            alphaLayerABB.paste(alphaLayerRTC, (0, 0))
            alphaLayerABB.paste(agradientL, (0, 0))
            alphaLayerABB.paste(
                agradientC.resize((overlap_size_x, overlap_size_y)), (0, 0)
            )

            # All-around layer
            alphaLayerAA = Image.new("L", (width, height), 255)
            alphaLayerAA.paste(alphaLayerABT, (0, 0))
            alphaLayerAA.paste(agradientT, (0, 0))
            alphaLayerAA.paste(
                agradientC.resize((overlap_size_x, overlap_size_y)), (0, 0)
            )
            alphaLayerAA.paste(
                agradientC.rotate(270).resize((overlap_size_x, overlap_size_y)),
                (width - overlap_size_x, 0),
            )

        # Clean up temporary gradients
        del agradientL
        del agradientT
        del agradientC

        def make_image():
            # Make main tiles -------------------------------------------------
            if embiggen_tiles:
                logger.info(f"Making {len(embiggen_tiles)} Embiggen tiles...")
            else:
                logger.info(
                    f"Making {(emb_tiles_x * emb_tiles_y)} Embiggen tiles ({emb_tiles_x}x{emb_tiles_y})..."
                )

            emb_tile_store = []
            # Although we could use the same seed for every tile for determinism, at higher strengths this may
            # produce duplicated structures for each tile and make the tiling effect more obvious
            # instead track and iterate a local seed we pass to Img2Img
            seed = self.seed
            seedintlimit = (
                np.iinfo(np.uint32).max - 1
            )  # only retreive this one from numpy

            for tile in range(emb_tiles_x * emb_tiles_y):
                # Don't iterate on first tile
                if tile != 0:
                    if seed < seedintlimit:
                        seed += 1
                    else:
                        seed = 0

                # Determine if this is a re-run and replace
                if embiggen_tiles and not tile in embiggen_tiles:
                    continue
                # Get row and column entries
                emb_row_i = tile // emb_tiles_x
                emb_column_i = tile % emb_tiles_x
                # Determine bounds to cut up the init image
                # Determine upper-left point
                if emb_column_i + 1 == emb_tiles_x:
                    left = initsuperwidth - width
                else:
                    left = round(emb_column_i * (width - overlap_size_x))
                if emb_row_i + 1 == emb_tiles_y:
                    top = initsuperheight - height
                else:
                    top = round(emb_row_i * (height - overlap_size_y))
                right = left + width
                bottom = top + height

                # Cropped image of above dimension (does not modify the original)
                newinitimage = initsuperimage.crop((left, top, right, bottom))
                # DEBUG:
                # newinitimagepath = init_img[0:-4] + f'_emb_Ti{tile}.png'
                # newinitimage.save(newinitimagepath)

                if embiggen_tiles:
                    logger.debug(
                        f"Making tile #{tile + 1} ({embiggen_tiles.index(tile) + 1} of {len(embiggen_tiles)} requested)"
                    )
                else:
                    logger.debug(f"Starting {tile + 1} of {(emb_tiles_x * emb_tiles_y)} tiles")

                # create a torch tensor from an Image
                newinitimage = np.array(newinitimage).astype(np.float32) / 255.0
                newinitimage = newinitimage[None].transpose(0, 3, 1, 2)
                newinitimage = torch.from_numpy(newinitimage)
                newinitimage = 2.0 * newinitimage - 1.0
                newinitimage = newinitimage.to(self.model.device)
                clear_cuda_cache = (
                    kwargs["clear_cuda_cache"] if "clear_cuda_cache" in kwargs else None
                )

                tile_results = gen_img2img.generate(
                    prompt,
                    iterations=1,
                    seed=seed,
                    sampler=sampler,
                    steps=steps,
                    cfg_scale=cfg_scale,
                    conditioning=conditioning,
                    ddim_eta=ddim_eta,
                    image_callback=None,  # called only after the final image is generated
                    step_callback=step_callback,  # called after each intermediate image is generated
                    width=width,
                    height=height,
                    init_image=newinitimage,  # notice that init_image is different from init_img
                    mask_image=None,
                    strength=strength,
                    clear_cuda_cache=clear_cuda_cache,
                )

                emb_tile_store.append(tile_results[0][0])
                # DEBUG (but, also has other uses), worth saving if you want tiles without a transparency overlap to manually composite
                # emb_tile_store[-1].save(init_img[0:-4] + f'_emb_To{tile}.png')
                del newinitimage

            # Sanity check we have them all
            if len(emb_tile_store) == (emb_tiles_x * emb_tiles_y) or (
                embiggen_tiles != [] and len(emb_tile_store) == len(embiggen_tiles)
            ):
                outputsuperimage = Image.new("RGBA", (initsuperwidth, initsuperheight))
                if embiggen_tiles:
                    outputsuperimage.alpha_composite(
                        initsuperimage.convert("RGBA"), (0, 0)
                    )
                for tile in range(emb_tiles_x * emb_tiles_y):
                    if embiggen_tiles:
                        if tile in embiggen_tiles:
                            intileimage = emb_tile_store.pop(0)
                        else:
                            continue
                    else:
                        intileimage = emb_tile_store[tile]
                    intileimage = intileimage.convert("RGBA")
                    # Get row and column entries
                    emb_row_i = tile // emb_tiles_x
                    emb_column_i = tile % emb_tiles_x
                    if emb_row_i == 0 and emb_column_i == 0 and not embiggen_tiles:
                        left = 0
                        top = 0
                    else:
                        # Determine upper-left point
                        if emb_column_i + 1 == emb_tiles_x:
                            left = initsuperwidth - width
                        else:
                            left = round(emb_column_i * (width - overlap_size_x))
                        if emb_row_i + 1 == emb_tiles_y:
                            top = initsuperheight - height
                        else:
                            top = round(emb_row_i * (height - overlap_size_y))
                        # Handle gradients for various conditions
                        # Handle emb_rerun case
                        if embiggen_tiles:
                            # top of image
                            if emb_row_i == 0:
                                if emb_column_i == 0:
                                    if (tile + 1) in embiggen_tiles:  # Look-ahead right
                                        if (
                                            tile + emb_tiles_x
                                        ) not in embiggen_tiles:  # Look-ahead down
                                            intileimage.putalpha(alphaLayerB)
                                        # Otherwise do nothing on this tile
                                    elif (
                                        tile + emb_tiles_x
                                    ) in embiggen_tiles:  # Look-ahead down only
                                        intileimage.putalpha(alphaLayerR)
                                    else:
                                        intileimage.putalpha(alphaLayerRBC)
                                elif emb_column_i == emb_tiles_x - 1:
                                    if (
                                        tile + emb_tiles_x
                                    ) in embiggen_tiles:  # Look-ahead down
                                        intileimage.putalpha(alphaLayerL)
                                    else:
                                        intileimage.putalpha(alphaLayerLBC)
                                else:
                                    if (tile + 1) in embiggen_tiles:  # Look-ahead right
                                        if (
                                            tile + emb_tiles_x
                                        ) in embiggen_tiles:  # Look-ahead down
                                            intileimage.putalpha(alphaLayerL)
                                        else:
                                            intileimage.putalpha(alphaLayerLBC)
                                    elif (
                                        tile + emb_tiles_x
                                    ) in embiggen_tiles:  # Look-ahead down only
                                        intileimage.putalpha(alphaLayerLR)
                                    else:
                                        intileimage.putalpha(alphaLayerABT)
                            # bottom of image
                            elif emb_row_i == emb_tiles_y - 1:
                                if emb_column_i == 0:
                                    if (tile + 1) in embiggen_tiles:  # Look-ahead right
                                        intileimage.putalpha(alphaLayerTaC)
                                    else:
                                        intileimage.putalpha(alphaLayerRTC)
                                elif emb_column_i == emb_tiles_x - 1:
                                    # No tiles to look ahead to
                                    intileimage.putalpha(alphaLayerLTC)
                                else:
                                    if (tile + 1) in embiggen_tiles:  # Look-ahead right
                                        intileimage.putalpha(alphaLayerLTaC)
                                    else:
                                        intileimage.putalpha(alphaLayerABB)
                            # vertical middle of image
                            else:
                                if emb_column_i == 0:
                                    if (tile + 1) in embiggen_tiles:  # Look-ahead right
                                        if (
                                            tile + emb_tiles_x
                                        ) in embiggen_tiles:  # Look-ahead down
                                            intileimage.putalpha(alphaLayerTaC)
                                        else:
                                            intileimage.putalpha(alphaLayerTB)
                                    elif (
                                        tile + emb_tiles_x
                                    ) in embiggen_tiles:  # Look-ahead down only
                                        intileimage.putalpha(alphaLayerRTC)
                                    else:
                                        intileimage.putalpha(alphaLayerABL)
                                elif emb_column_i == emb_tiles_x - 1:
                                    if (
                                        tile + emb_tiles_x
                                    ) in embiggen_tiles:  # Look-ahead down
                                        intileimage.putalpha(alphaLayerLTC)
                                    else:
                                        intileimage.putalpha(alphaLayerABR)
                                else:
                                    if (tile + 1) in embiggen_tiles:  # Look-ahead right
                                        if (
                                            tile + emb_tiles_x
                                        ) in embiggen_tiles:  # Look-ahead down
                                            intileimage.putalpha(alphaLayerLTaC)
                                        else:
                                            intileimage.putalpha(alphaLayerABR)
                                    elif (
                                        tile + emb_tiles_x
                                    ) in embiggen_tiles:  # Look-ahead down only
                                        intileimage.putalpha(alphaLayerABB)
                                    else:
                                        intileimage.putalpha(alphaLayerAA)
                        # Handle normal tiling case (much simpler - since we tile left to right, top to bottom)
                        else:
                            if emb_row_i == 0 and emb_column_i >= 1:
                                intileimage.putalpha(alphaLayerL)
                            elif emb_row_i >= 1 and emb_column_i == 0:
                                if (
                                    emb_column_i + 1 == emb_tiles_x
                                ):  # If we don't have anything that can be placed to the right
                                    intileimage.putalpha(alphaLayerT)
                                else:
                                    intileimage.putalpha(alphaLayerTaC)
                            else:
                                if (
                                    emb_column_i + 1 == emb_tiles_x
                                ):  # If we don't have anything that can be placed to the right
                                    intileimage.putalpha(alphaLayerLTC)
                                else:
                                    intileimage.putalpha(alphaLayerLTaC)
                    # Layer tile onto final image
                    outputsuperimage.alpha_composite(intileimage, (left, top))
            else:
                logger.error(
                    "Could not find all Embiggen output tiles in memory? Something must have gone wrong with img2img generation."
                )

            # after internal loops and patching up return Embiggen image
            return outputsuperimage

        # end of function declaration
        return make_image
