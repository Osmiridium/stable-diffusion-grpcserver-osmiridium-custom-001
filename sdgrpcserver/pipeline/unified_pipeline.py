import inspect, traceback, math
import time
from mimetypes import init
from typing import Callable, List, Tuple, Optional, Union, Literal, NewType

import numpy as np
import torch

import torchvision
import torchvision.transforms as T

from torch.profiler import profile, record_function, ProfilerActivity

import PIL
from tqdm.auto import tqdm
from transformers import CLIPFeatureExtractor, CLIPModel, CLIPTextModel, CLIPTokenizer

from diffusers.configuration_utils import FrozenDict
from diffusers.models import AutoencoderKL, UNet2DConditionModel
from diffusers.pipeline_utils import DiffusionPipeline
from diffusers.schedulers import LMSDiscreteScheduler, PNDMScheduler
from diffusers.schedulers.scheduling_utils import SchedulerMixin
from diffusers.utils import deprecate, logging
from diffusers.pipelines.stable_diffusion import StableDiffusionPipelineOutput
from diffusers.pipelines.stable_diffusion.safety_checker import StableDiffusionSafetyChecker

from sdgrpcserver import images
from sdgrpcserver.pipeline.schedulers.scheduling_dpmsolver_multistep import DPMSolverMultistepScheduler
from sdgrpcserver.pipeline.kschedulers.scheduling_utils import KSchedulerMixin
from sdgrpcserver.pipeline.text_embedding import *

from sdgrpcserver.pipeline.attention_replacer import replace_cross_attention
from sdgrpcserver.pipeline.models.memory_efficient_cross_attention import has_xformers, MemoryEfficientCrossAttention
from sdgrpcserver.pipeline.models.structured_cross_attention import StructuredCrossAttention

from sdgrpcserver.resize_right import resize_right

try:
    from nonfree import tome_patcher
except:
    tome_patcher = None

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name

enabled_debug_latents = [
    # "initial"
    # "step",
    # "mask",
    # "initnoise"
]

def write_debug_latents(vae, label, i, latents):
    if label not in enabled_debug_latents: return

    stage_latents = 1 / 0.18215 * latents
    stage_image = vae.decode(stage_latents).sample
    stage_image = (stage_image / 2 + 0.5).clamp(0, 1).cpu()

    for j, pngBytes in enumerate(images.toPngBytes(stage_image)):
        with open(f"/tests/out/debug-{label}-{j}-{i}.png", "wb") as f:
            f.write(pngBytes)

def set_requires_grad(model, value):
    for param in model.parameters():
        param.requires_grad = value

class NoisePredictor:

    def __init__(self, pipeline, text_embeddings):
        self.pipeline = pipeline
        self.text_embeddings = text_embeddings

    def __call__(self, unet, latents, i, t, sigma = None, second = False):
        if isinstance(self.pipeline.scheduler, KSchedulerMixin): 
            if sigma is None: sigma = self.pipeline.scheduler.sigmas[i] 
            # the model input needs to be scaled to match the continuous ODE formulation in K-LMS
            latents = latents / ((sigma**2 + 1) ** 0.5)
        else:
            latents = self.pipeline.scheduler.scale_model_input(latents, t)

        # predict the noise residual
        noise_pred = unet(latents, t, encoder_hidden_states=self.text_embeddings).sample

        return noise_pred

class GuidedNoisePredictor(NoisePredictor):

    def __init__(self, pipeline, text_embeddings, guidance_scale):
        super().__init__(pipeline, text_embeddings)
        self.guidance_scale = guidance_scale

    def __call__(self, unet, latents, i, t, sigma = None, second = False):
        # expand the latents if we are doing classifier free guidance
        latents = torch.cat([latents] * 2)

        noise_pred = super().__call__(unet, latents, i, t, sigma = sigma)


        # perform guidance
        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
        noise_pred = noise_pred_uncond + self.guidance_scale * (noise_pred_text - noise_pred_uncond)

        return noise_pred

class MakeCutouts(torch.nn.Module):
    def __init__(self, cut_size, cut_power=1.0, generators=None):
        super().__init__()

        self.cut_size = cut_size
        self.cut_power = cut_power
        self.generators = generators

    def forward(self, pixel_values, num_cutouts):
        sideY, sideX = pixel_values.shape[2:4]
        max_size = min(sideX, sideY)
        min_size = min(sideX, sideY, self.cut_size)
        cutouts = []

        # To maintain batch-independance, we split out per batch, since each batch has a different generator
        # This also keeps the output grouped by batch (b1c1, b1c2, b1c3, ..., b2c1, b2c2, ...) whereas otherwise
        # it would be grouped by cutout (b1c1, b2c1, b3c1, ....., b1c2, b2c2, ...)
        for generator, batch_pixels in zip(self.generators, pixel_values.split(1)):
            for _ in range(num_cutouts):
                size = int(torch.rand([], generator=generator, device=generator.device) ** self.cut_power * (max_size - min_size) + min_size)
                offsetx = torch.randint(0, sideX - size + 1, (), generator=generator, device=generator.device)
                offsety = torch.randint(0, sideY - size + 1, (), generator=generator, device=generator.device)
                cutout = batch_pixels[:, :, offsety : offsety + size, offsetx : offsetx + size]

                cutouts.append(resize_right.resize(cutout, out_shape=(self.cut_size, self.cut_size), pad_mode='reflect').to(cutout.dtype))
        
        return torch.cat(cutouts)


def spherical_dist_loss(x, y):
    x = torch.nn.functional.normalize(x, dim=-1)
    y = torch.nn.functional.normalize(y, dim=-1)
    return (x - y).norm(dim=-1).div(2).arcsin().pow(2).mul(2)

class ApproximateDecoder:
    """Decodes latent data to an approximate representation in RGB.
    Values determined experimentally for Stable Diffusion 1.4.
    See https://discuss.huggingface.co/t/decoding-latents-to-rgb-without-upscaling/23204/2
    """

    # grayscale_factors = torch.tensor([
    #    #    R       G       B
    #    [ 0.342,  0.341,  0.343 ], # L1
    #    [ 0.342,  0.342,  0.340 ], # L2
    #    [-0.110, -0.110, -0.113 ], # L3
    #    [-0.208, -0.209, -0.208 ]  # L4
    # ])

    def __init__(self, device: torch.device, dtype: torch.dtype):
        self.latent_rgb_factors = torch.tensor([
            #   R        G        B
            [0.298, 0.207, 0.208],  # L1
            [0.187, 0.286, 0.173],  # L2
            [-0.158, 0.189, 0.264],  # L3
            [-0.184, -0.271, -0.473],  # L4
        ], dtype=dtype, device=device)

    @classmethod
    def for_pipeline(cls, pipeline: "diffusers.DiffusionPipeline"):
        return cls(device=pipeline.device, dtype=pipeline.unet.dtype)

    def __call__(self, latents):
        """Get an RGB JPEG representation of the latent data."""
        return torch.einsum('...lhw,lr -> ...rhw', latents, self.latent_rgb_factors)

CLIP_NO_CUTOUTS_TYPE = Literal[False, True, "vae", "approx"]

class ClipGuidedNoisePredictor:

    def __init__(self, pipeline, text_embeddings, guidance_scale, text_embeddings_clip, clip_guidance_scale, clip_gradient_threshold, vae_cutouts, approx_cutouts, no_cutouts, generator):
        self.pipeline = pipeline
        self.guidance_scale = guidance_scale
        self.text_embeddings_u, self.text_embeddings_g = text_embeddings.chunk(2)

        self.text_embeddings_clip = text_embeddings_clip
        self.clip_guidance_scale = clip_guidance_scale
        self.clip_gradient_threshold = clip_gradient_threshold
        self.vae_cutouts = vae_cutouts
        self.approx_cutouts = approx_cutouts
        self.no_cutouts = no_cutouts

        self.normalize = T.Normalize(mean=self.pipeline.feature_extractor.image_mean, std=self.pipeline.feature_extractor.image_std)
        self.normalizeB = T.Normalize(mean=self.pipeline.feature_extractor.image_mean[2], std=self.pipeline.feature_extractor.image_std[2])

        self.make_cutouts = MakeCutouts(self.pipeline.feature_extractor.size // 8, generators=generator)
        self.make_cutouts_rgb = MakeCutouts(self.pipeline.feature_extractor.size, generators=generator)

        self.approx_decoder = ApproximateDecoder(device = self.pipeline.device, dtype = text_embeddings.dtype)

        self.generator = generator

        self.lossavg = []
        self.flatloss = False

    def __call__(self, unet, latents, i, t, sigma = None, second = False):
        if sigma is None and isinstance(self.pipeline.scheduler, KSchedulerMixin): 
            sigma = self.pipeline.scheduler.sigmas[i] 

        if isinstance(self.pipeline.scheduler, KSchedulerMixin): 
            latent_model_input = latents / ((sigma**2 + 1) ** 0.5)
        else:
            latent_model_input = self.pipeline.scheduler.scale_model_input(latents, t)

        noise_pred_u = unet(latent_model_input, t, encoder_hidden_states=self.text_embeddings_u).sample

        if second or self.flatloss:
            noise_pred_g = unet(latent_model_input, t, encoder_hidden_states=self.text_embeddings_g).sample
            return  noise_pred_u + self.guidance_scale * (noise_pred_g - noise_pred_u)

        # perform guidance - in a seperate function as this contains the bits where grad is needed
        grads, noise_pred_g = self.cond_fn(
            unet,
            latents,
            t,
            i,
            self.text_embeddings_g,
            self.text_embeddings_clip,
            self.clip_guidance_scale,
            self.vae_cutouts,
            self.approx_cutouts,
            self.no_cutouts,
            sigma
        )

        if len(self.lossavg) > 10:
            # Calculate gradient for loss
            x = np.linspace(0,1,10)
            X = np.vstack([x, np.ones(len(x))]).T
            y = np.asarray(self.lossavg[-10:])

            m, c = np.linalg.lstsq(X, y, rcond=None)[0]
            if abs(m) < self.clip_gradient_threshold:
                self.flatloss = True

        noise_pred_orig = noise_pred_u + self.guidance_scale * (noise_pred_g - noise_pred_u)

        if isinstance(self.pipeline.scheduler, KSchedulerMixin): 
            latents = latents + grads * (sigma**2)
            return noise_pred_orig, latents
        else:
            # Todo: Don't duplicate this bit from cond_fn
            alpha_prod_t = self.pipeline.scheduler.alphas_cumprod[t]
            beta_prod_t = 1 - alpha_prod_t
            return noise_pred_orig - torch.sqrt(beta_prod_t) * grads

    @torch.enable_grad()
    def cond_fn(
        self,
        unet,
        latents,
        timestep,
        index,
        text_embeddings_g,
        text_embeddings_clip,
        clip_guidance_scale,
        vae_cutouts,
        approx_cutouts,
        no_cutouts,
        sigma
    ):
        num_cutouts = vae_cutouts + approx_cutouts
        batch_total = latents.shape[0]

        latents = latents.detach().requires_grad_()
        resized_latents = latents

        if isinstance(self.pipeline.scheduler, KSchedulerMixin):
            latent_model_input = resized_latents / ((sigma**2 + 1) ** 0.5)
        else:
            latent_model_input = resized_latents        

        # predict the noise residual
        noise_pred_g = unet(latent_model_input, timestep, encoder_hidden_states=text_embeddings_g).sample

        if isinstance(self.pipeline.scheduler, KSchedulerMixin):
            # And calculate the output
            sample = resized_latents - sigma * noise_pred_g
            clip_guidance_scale = clip_guidance_scale / 10 # Adjust to match to non-KScheduler effect. Determined through experiment.
        else:
            alpha_prod_t = self.pipeline.scheduler.alphas_cumprod[timestep]
            beta_prod_t = 1 - alpha_prod_t
            # compute predicted original sample from predicted noise also called
            # "predicted x_0" of formula (12) from https://arxiv.org/pdf/2010.02502.pdf
            pred_original_sample = (resized_latents - beta_prod_t ** (0.5) * noise_pred_g) / alpha_prod_t ** (0.5)

            fac = torch.sqrt(beta_prod_t)
            sample = pred_original_sample * (fac) + resized_latents * (1 - fac)

        # Convert sample (the whole denoised next image in latent space) to a set of images we check using CLIP
        # For VAE we crop / resize _before_ VAE to limit size of grads. 
        # For Approximation we crop / resize after fake-VAE to try and maximise signal (since the approximation doesn't include an automatic 8x upscale)

        if no_cutouts:
            if no_cutouts == "approx":
                image = self.approx_decoder(sample)
                image = T.Resize(self.pipeline.feature_extractor.size)(image)
            else:
                sample = T.Resize(self.pipeline.feature_extractor.size // 8)(sample)
                sample = 1 / 0.18215 * sample
                image = self.pipeline.vae.decode(sample).sample

        else:
            image = None

            if approx_cutouts:
                image = self.approx_decoder(sample)
                image = resize_right.resize(image, out_shape=(sample.shape[2]*8, sample.shape[3]*8), pad_mode='reflect').to(sample.dtype)
                image = self.make_cutouts_rgb(image, approx_cutouts)

            if vae_cutouts:
                sample2 = self.make_cutouts(sample, vae_cutouts)
                sample2 = 1 / 0.18215 * sample2
                image2 = self.pipeline.vae.decode(sample2).sample

                if image is None:
                    image = image2
                else:
                    # image and image2 are batch-grouped. We need to interleave them so they are still batch groups after
                    # [b1c1, b1c2, b2c1, b2c2] + [b1c3, b1c4, b2c3, b2c4] => [b1c1, b1c2, b1c3, b1c4, b2c1, b2c2, b2c3, b2c4]
                    # First, split into 5D b cut c h w tensors
                    image = image.view(batch_total, approx_cutouts, *image.shape[-3:])
                    image2 = image.view(batch_total, vae_cutouts, *image2.shape[-3:])
                    # Now stack on the cut dimension, so we get a 6D tensor b cutgroup cut c h w
                    image = torch.stack([image, image2], dim=1)
                    # Then collapse down into 4D b*cut c h w
                    image = image.view(batch_total * num_cutouts, *image.shape[-3:])

        image = (image / 2 + 0.5).clamp(0, 1)
        image = self.normalize(image)

        image_embeddings_clip = self.pipeline.clip_model.get_image_features(image)

        if no_cutouts:
            loss = spherical_dist_loss(image_embeddings_clip, text_embeddings_clip).mean()
        else:
            text_embeddings_input = text_embeddings_clip.repeat_interleave(num_cutouts, dim=0)
            dists = spherical_dist_loss(image_embeddings_clip, text_embeddings_input)
            dists = dists.view([num_cutouts, latents.shape[0], -1])
            loss = dists.sum(2).mean(0).sum() 

        self.lossavg.append(float(loss))

        grads = -torch.autograd.grad(loss * (clip_guidance_scale * 500), latents)[0]
        return grads, noise_pred_g


class UnifiedMode(object):

    def __init__(self, noise_predictor, **_):
        self.t_start = 0
        self.noise_predictor = noise_predictor

    def generateLatents(self):
        raise NotImplementedError('Subclasses must implement')

    def unet(self, latent_model_input, t, encoder_hidden_states):
        return self.pipeline.unet(latent_model_input, t, encoder_hidden_states=encoder_hidden_states)

    def latentStep(self, latents, i, t, steppos):
        return latents

    def noise_predict(self, latents, i, t, sigma = None):
        return self.noise_predictor(self.unet, latents, i, t, sigma = sigma)

class Txt2imgMode(UnifiedMode):

    def __init__(self, pipeline, generator, height, width, latents_dtype, batch_total, **kwargs):
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        super().__init__(**kwargs)

        self.device = pipeline.device
        self.scheduler = pipeline.scheduler
        self.pipeline = pipeline

        self.generators = generator if isinstance(generator, list) else [generator] * batch_total

        self.latents_dtype = latents_dtype
        self.latents_shape = (
            batch_total, 
            pipeline.unet.in_channels, 
            height // 8, 
            width // 8
        )

    def generateLatents(self):
        # Unlike in other pipelines, latents need to be generated in the target device
        # for 1-to-1 results reproducibility with the CompVis implementation.
        # However this currently doesn't work in `mps`.
        latents = torch.cat([
            torch.randn((1, *self.latents_shape[1:]), generator=generator, device=generator.device, dtype=self.latents_dtype) 
            for generator in self.generators
        ], dim=0)
        
        latents = latents.to(self.device)

        # scale the initial noise by the standard deviation required by the scheduler
        if isinstance(self.scheduler, KSchedulerMixin): 
            return latents * self.scheduler.sigmas[0]
        else:
            return latents * self.scheduler.init_noise_sigma

class Img2imgMode(UnifiedMode):

    def __init__(self, pipeline, generator, init_image, latents_dtype, batch_total, num_inference_steps, strength, **kwargs):
        if strength < 0 or strength > 1:
            raise ValueError(f"The value of strength should in [0.0, 1.0] but is {strength}")
        
        super().__init__(**kwargs)

        self.device = pipeline.device
        self.scheduler = pipeline.scheduler
        self.pipeline = pipeline

        self.generators = generator if isinstance(generator, list) else [generator] * batch_total

        self.latents_dtype = latents_dtype
        self.batch_total = batch_total
        
        self.offset = self.scheduler.config.get("steps_offset", 0)
        self.init_timestep = int(num_inference_steps * strength) + self.offset
        self.init_timestep = min(self.init_timestep, num_inference_steps)
        self.t_start = max(num_inference_steps - self.init_timestep + self.offset, 0)

        if isinstance(init_image, PIL.Image.Image):
            self.init_image = self.preprocess(init_image)
        else:
            self.init_image = self.preprocess_tensor(init_image)

    def preprocess(self, image):
        w, h = image.size
        w, h = map(lambda x: x - x % 32, (w, h))  # resize to integer multiple of 32
        image = image.resize((w, h), resample=PIL.Image.LANCZOS)
        image = np.array(image).astype(np.float32) / 255.0
        image = image[None].transpose(0, 3, 1, 2)
        image = torch.from_numpy(image)
        return 2.0 * image - 1.0

    def preprocess_tensor(self, tensor):
        # Make sure it's BCHW not just CHW
        if tensor.ndim == 3: tensor = tensor[None, ...]
        # Strip any alpha
        tensor = tensor[:, [0,1,2]]
        # Adjust to -1 .. 1
        tensor = 2.0 * tensor - 1.0
        # Done
        return tensor

    def _convertToLatents(self, image, mask=None):
        """
        Convert an RGB image in standard tensor (BCHW, 0..1) format
        to latents, optionally with pre-masking

        The output will have a batch for each generator / batch_total
        
        If passed, mask must be the same res as the image, and
        is "black replace, white keep" format (0R1K)
        """
        if image.shape[0] != 1: 
            print(
                "Warning: image passed to convertToLatents has more than one batch. "
                "This is probably a mistake"
            )

        if mask is not None and mask.shape[0] != 1: 
            print(
                "Warning: mask passed to convertToLatents has more than one batch. "
                "This is probably a mistake"
            )

        image = image.to(device=self.device, dtype=self.latents_dtype)
        if mask is not None: image = image * (mask > 0.5)

        dist = self.pipeline.vae.encode(image).latent_dist

        latents = torch.cat([
            dist.sample(generator=generator)
            for generator in self.generators
        ], dim=0)

        latents = latents.to(self.device)
        latents = 0.18215 * latents

        return latents

    def _buildInitialLatents(self):
        return self._convertToLatents(self.init_image)

    def _getSchedulerNoiseTimestep(self, i, t = None):
        """Figure out the timestep to pass to scheduler.add_noise
        If it's an old-style scheduler:
          - return the index as a single integer tensor

        If it's a new-style scheduler:
          - if we know the timestep use it
          - otherwise look up the timestep in the scheduler
          - either way, return a tensor * batch_total on our device
        """
        if isinstance(self.scheduler, KSchedulerMixin): 
            return torch.tensor(i)
        else:
            timesteps = t if t != None else self.scheduler.timesteps[i]
            return torch.tensor([timesteps] * self.batch_total, device=self.device)

    def _addInitialNoise(self, latents):
        # NOTE: We run K_LMS in float32, because it seems to have problems with float16
        noise_dtype=torch.float32 if isinstance(self.scheduler, LMSDiscreteScheduler) else self.latents_dtype

        image_noise = torch.cat([
            torch.randn((1, *latents.shape[1:]), generator=generator, device=generator.device, dtype=noise_dtype)
            for generator in self.generators
        ])

        self.image_noise = image_noise.to(self.device)

        result = self.scheduler.add_noise(latents.to(noise_dtype), self.image_noise, self._getSchedulerNoiseTimestep(self.t_start))
        return result.to(self.latents_dtype) # Old schedulers return float32, and we force K_LMS into float32, but we need to return float16

    def generateLatents(self):
        init_latents = self._buildInitialLatents()
        init_latents = self._addInitialNoise(init_latents)
        return init_latents

class MaskProcessorMixin(object):

    def preprocess_mask(self, mask, inputIs0K1R=True):
        """
        Load a mask from a PIL image
        """
        mask = mask.convert("L")
        mask = np.array(mask).astype(np.float32) / 255.0
        return self.preprocess_mask_tensor(torch.from_numpy(mask), inputIs0K1R=inputIs0K1R)

    def preprocess_mask_tensor(self, tensor, inputIs0K1R=True):
        """
        Preprocess a tensor in 1CHW 0..1 format into a mask in
        11HW 0..1 0R1K format
        """

        if tensor.ndim == 3: tensor = tensor[None, ...]
        # Create a single channel, from whichever the first channel is (L or R)
        tensor = tensor[:, [0]]
        # Invert if input is 0K1R
        if inputIs0K1R: tensor = 1 - tensor
        # Done
        return tensor

    def mask_to_latent_mask(self, mask):
        # Downsample by a factor of 1/8th
        mask = T.functional.resize(mask, [mask.shape[2]//8, mask.shape[3]//8], T.InterpolationMode.NEAREST)
        # And make 4 channel to match latent shape
        mask = mask[:, [0, 0, 0, 0]]
        # Done
        return mask

    def round_mask(self, mask, threshold=0.5):
        """
        Round mask to either 0 or 1 based on threshold (by default, evenly)
        """
        mask = mask.clone()
        mask[mask >= threshold] = 1
        mask[mask < 1] = 0
        return mask

    def round_mask_high(self, mask):
        """
        Round mask so anything above 0 is rounded up to 1
        """
        return self.round_mask(mask, 0.001)

    def round_mask_low(self, mask):
        """
        Round mask so anything below 1 is rounded down to 0
        """
        return self.round_mask(mask, 0.999)

class OriginalInpaintMode(Img2imgMode, MaskProcessorMixin):

    def __init__(self, mask_image, **kwargs):
        super().__init__(**kwargs)

        if isinstance(mask_image, PIL.Image.Image):
            self.mask_image = self.preprocess_mask(mask_image)
        else:
            self.mask_image = self.preprocess_mask_tensor(mask_image)

        self.mask = self.mask_image.to(device=self.device, dtype=self.latents_dtype)
        self.mask = torch.cat([self.mask] * self.batch_total)

    def generateLatents(self):
        init_latents = self._buildInitialLatents()

        self.init_latents_orig = init_latents

        init_latents = self._addInitialNoise(init_latents)
        return init_latents

    def latentStep(self, latents, i, t, steppos):
        # masking
        init_latents_proper = self.scheduler.add_noise(self.init_latents_orig, self.image_noise, torch.tensor([t]))
        return (init_latents_proper * self.mask) + (latents * (1 - self.mask))

class RunwayInpaintMode(UnifiedMode):

    def __init__(self, pipeline, generator, init_image, mask_image, latents_dtype, batch_total, num_inference_steps, strength, do_classifier_free_guidance, **kwargs):
        if strength < 0 or strength > 1:
            raise ValueError(f"The value of strength should in [0.0, 1.0] but is {strength}")
        
        super().__init__(**kwargs)

        self.device = pipeline.device
        self.scheduler = pipeline.scheduler
        self.pipeline = pipeline

        self.generators = generator if isinstance(generator, list) else [generator] * batch_total

        if isinstance(init_image, PIL.Image.Image):
            mask, masked_image = self._prepare_mask_and_masked_image(init_image, mask_image)
        else:
            mask, masked_image = self._prepare_mask_and_masked_image_tensor(init_image, mask_image)

        height = mask_image.shape[2]
        width = mask_image.shape[3]

        self.latents_dtype = latents_dtype
        self.num_channels_latents = self.pipeline.vae.config.latent_channels
        self.latents_shape = (
            batch_total, 
            self.num_channels_latents, 
            height // 8, 
            width // 8
        )

        self.batch_total = batch_total
        
        self.offset = self.scheduler.config.get("steps_offset", 0)
        self.init_timestep = int(num_inference_steps * strength) + self.offset
        self.init_timestep = min(self.init_timestep, num_inference_steps)
        self.t_start = max(num_inference_steps - self.init_timestep + self.offset, 0)

        # -- Stage 2 prep --

        # prepare mask and masked_image
        mask = mask.to(device=self.device, dtype=self.latents_dtype)
        masked_image = masked_image.to(device=self.device, dtype=self.latents_dtype)

        # resize the mask to latents shape as we concatenate the mask to the latents
        mask = torch.nn.functional.interpolate(mask, size=(height // 8, width // 8))

        # encode the mask image into latents space so we can concatenate it to the latents
        masked_image_latent_dist = self.pipeline.vae.encode(masked_image).latent_dist
        
        masked_image_latents = torch.cat([
            masked_image_latent_dist.sample(generator=generator)
            for generator in self.generators
        ], dim=0)
        
        masked_image_latents = 0.18215 * masked_image_latents

        # duplicate mask and masked_image_latents for each generation per prompt, using mps friendly method
        mask = mask.repeat(batch_total, 1, 1, 1)
        #masked_image_latents = masked_image_latents.repeat(num_images_per_prompt, 1, 1, 1)

        mask = torch.cat([mask] * 2) if do_classifier_free_guidance else mask
        masked_image_latents = (
            torch.cat([masked_image_latents] * 2) if do_classifier_free_guidance else masked_image_latents
        )

        num_channels_mask = mask.shape[1]
        num_channels_masked_image = masked_image_latents.shape[1]

        if self.num_channels_latents + num_channels_mask + num_channels_masked_image != self.pipeline.inpaint_unet.config.in_channels:
            raise ValueError(
                f"Incorrect configuration settings! The config of `pipeline.inpaint_unet`: {self.pipeline.inpaint_unet.config} expects"
                f" {self.pipeline.inpaint_unet.config.in_channels} but received `num_channels_latents`: {self.num_channels_latents} +"
                f" `num_channels_mask`: {num_channels_mask} + `num_channels_masked_image`: {num_channels_masked_image}"
                f" = {self.num_channels_latents+num_channels_masked_image+num_channels_mask}. Please verify the config of"
                " `pipeline.unet` or your `mask_image` or `image` input."
            )

        self.mask = mask
        self.masked_image_latents = masked_image_latents

    def _prepare_mask_and_masked_image(image, mask):
        image = np.array(image.convert("RGB"))
        image = image[None].transpose(0, 3, 1, 2)
        image = torch.from_numpy(image).to(dtype=torch.float32) / 255

        mask = np.array(mask.convert("L"))
        mask = mask.astype(np.float32) / 255.0
        mask = torch.from_numpy(mask)

        return self._prepare_mask_and_masked_image_tensor(image, mask)

    def _prepare_mask_and_masked_image_tensor(self, image_tensor, mask_tensor):
        # Make sure it's BCHW not just CHW
        if image_tensor.ndim == 3: image_tensor = image_tensor[None, ...]
        # Strip any alpha
        image_tensor = image_tensor[:, [0,1,2]]
        # Adjust to -1 .. 1
        image_tensor = 2.0 * image_tensor - 1.0

        # Make sure it's BCHW not just CHW
        if mask_tensor.ndim == 3: mask_tensor = mask_tensor[None, ...]
        # Ensure single channel
        mask_tensor = mask_tensor[:, [0]]
        # Harden
        mask_tensor[mask_tensor < 0.5] = 0
        mask_tensor[mask_tensor >= 0.5] = 1

        masked_image_tensor = image_tensor * (mask_tensor < 0.5)

        return mask_tensor, masked_image_tensor

    def generateLatents(self):
        latents = torch.cat([
            torch.randn((1, *self.latents_shape[1:]), generator=generator, device=generator.device, dtype=self.latents_dtype) 
            for generator in self.generators
        ], dim=0)

        latents = latents.to(self.device)

        # scale the initial noise by the standard deviation required by the scheduler
        if isinstance(self.scheduler, KSchedulerMixin): 
            return latents * self.scheduler.sigmas[0]
        else:
            return latents * self.scheduler.init_noise_sigma

    def unet(self, latent_model_input, t, encoder_hidden_states):
        latent_model_input = torch.cat([latent_model_input, self.mask, self.masked_image_latents], dim=1)
        return self.pipeline.inpaint_unet(latent_model_input, t, encoder_hidden_states=encoder_hidden_states)

class EnhancedInpaintMode(Img2imgMode, MaskProcessorMixin):

    def __init__(self, mask_image, num_inference_steps, strength, **kwargs):
        # Check strength
        if strength < 0 or strength > 2:
            raise ValueError(f"The value of strength should in [0.0, 2.0] but is {strength}")

        # When strength > 1, we start allowing the protected area to change too. Remember that and then set strength
        # to 1 for parent class
        self.fill_with_shaped_noise = strength >= 1.0

        self.shaped_noise_strength = min(2 - strength, 1)
        self.mask_scale = 1

        strength = min(strength, 1)

        super().__init__(strength=strength, num_inference_steps=num_inference_steps, **kwargs)

        self.num_inference_steps = num_inference_steps

        # Load mask in 1K0D, 1CHW L shape
        if isinstance(mask_image, PIL.Image.Image):
            self.mask = self.preprocess_mask(mask_image)
        else:
            self.mask = self.preprocess_mask_tensor(mask_image)

        self.mask = self.mask.to(device=self.device, dtype=self.latents_dtype)

        # Remove any excluded pixels (0)
        high_mask = self.round_mask_high(self.mask)
        self.init_latents_orig = self._convertToLatents(self.init_image, high_mask)
        #low_mask = self.round_mask_low(self.mask)
        #blend_mask = self.mask * self.mask_scale

        self.latent_mask = self.mask_to_latent_mask(self.mask)
        self.latent_mask = torch.cat([self.latent_mask] * self.batch_total)

        self.latent_high_mask = self.round_mask_high(self.latent_mask)
        self.latent_low_mask = self.round_mask_low(self.latent_mask)
        self.latent_blend_mask = self.latent_mask * self.mask_scale


    def _matchToSD(self, tensor, targetSD):
        # Normalise tensor to -1..1
        tensor=tensor-tensor.min()
        tensor=tensor.div(tensor.max())
        tensor=tensor*2-1

        # Caculate standard deviation
        sd = tensor.std()
        return tensor * targetSD / sd

    def _matchToSamplerSD(self, tensor):
        if isinstance(self.scheduler, KSchedulerMixin): 
            targetSD = self.scheduler.sigmas[0]
        else:
            targetSD = self.scheduler.init_noise_sigma

        return _matchToSD(self, tensor, targetSD)

    def _matchNorm(self, tensor, like, cf=1):
        # Normalise tensor to 0..1
        tensor=tensor-tensor.min()
        tensor=tensor.div(tensor.max())

        # Then match range to like
        norm_range = (like.max() - like.min()) * cf
        norm_min = like.min() * cf
        return tensor * norm_range + norm_min


    def _fillWithShapedNoise(self, init_latents, noise_mode=5):
        """
        noise_mode sets the noise distribution prior to convolution with the latent

        0: normal, matched to latent, 1: cauchy, matched to latent, 2: log_normal, 
        3: standard normal (mean=0, std=1), 4: normal to scheduler SD
        5: random shuffle (does not convolve afterwards)
        """

        # HERE ARE ALL THE THINGS THAT GIVE BETTER OR WORSE RESULTS DEPENDING ON THE IMAGE:
        noise_mask_factor=1 # (1) How much to reduce noise during mask transition
        lmask_mode=3 # 3 (high_mask) seems consistently good. Options are 0 = none, 1 = low mask, 2 = mask as passed, 3 = high mask
        nmask_mode=0 # 1 or 3 seem good, 3 gives good blends slightly more often
        fft_norm_mode="ortho" # forward, backward or ortho. Doesn't seem to affect results too much

        # 0 == to sampler requested std deviation, 1 == to original image distribution
        match_mode=2

        def latent_mask_for_mode(mode):
            if mode == 1: return self.latent_low_mask
            elif mode == 2: return self.latent_mask
            else: return self.latent_high_mask

        # Current theory: if we can match the noise to the image latents, we get a nice well scaled color blend between the two.
        # The nmask mostly adjusts for incorrect scale. With correct scale, nmask hurts more than it helps

        # noise_mode = 0 matches well with nmask_mode = 0
        # nmask_mode = 1 or 3 matches well with noise_mode = 1 or 3

        # Only consider the portion of the init image that aren't completely masked
        masked_latents = init_latents

        if lmask_mode > 0:
            latent_mask = latent_mask_for_mode(lmask_mode)
            masked_latents = masked_latents * latent_mask

        batch_noise = []

        for generator, split_latents in zip(self.generators, masked_latents.split(1)):
            # Generate some noise
            noise = torch.zeros_like(split_latents)
            if noise_mode == 0 and noise_mode < 1: noise = noise.normal_(generator=generator, mean=split_latents.mean(), std=split_latents.std())
            elif noise_mode == 1 and noise_mode < 2: noise = noise.cauchy_(generator=generator, median=split_latents.median(), sigma=split_latents.std())
            elif noise_mode == 2:
                noise = noise.log_normal_(generator=generator)
                noise = noise - noise.mean()
            elif noise_mode == 3: noise = noise.normal_(generator=generator)
            elif noise_mode == 4:
                if isinstance(self.scheduler, KSchedulerMixin):
                    targetSD = self.scheduler.sigmas[0]
                else:
                    targetSD = self.scheduler.init_noise_sigma

                noise = noise.normal_(generator=generator, mean=0, std=targetSD)
            elif noise_mode == 5:
                # Seed the numpy RNG from the batch generator, so it's consistent
                npseed = torch.randint(low=0, high=torch.iinfo(torch.int32).max, size=[1], generator=generator, device=generator.device, dtype=torch.int32).cpu()
                npgen = np.random.default_rng(npseed.numpy())
                # Fill each channel with random pixels selected from the good portion
                # of the channel. I wish there was a way to do this in PyTorch :shrug:
                channels = []
                for channel in split_latents.split(1, dim=1):
                    good_pixels = channel.masked_select(latent_mask[[0], [0]].ge(0.5))
                    np_mixed = npgen.choice(good_pixels.cpu().numpy(), channel.shape)
                    channels.append(torch.from_numpy(np_mixed).to(noise.device).to(noise.dtype))

                # In noise mode 5 we don't convolve. The pixel shuffled noise is already extremely similar to the original in tone. 
                # We allow the user to request some portion is uncolored noise to allow outpaints that differ greatly from original tone
                # (with an increasing risk of image discontinuity)
                noise = noise.to(generator.device).normal_(generator=generator).to(noise.device)
                noise = noise * (1-self.shaped_noise_strength) + torch.cat(channels, dim=1) * self.shaped_noise_strength

                batch_noise.append(noise)
                continue

            elif noise_mode == 6:
                noise = torch.ones_like(split_latents)


            # Make the noise less of a component of the convolution compared to the latent in the unmasked portion
            if nmask_mode > 0:
                noise_mask = latent_mask_for_mode(nmask_mode)
                noise = noise.mul(1-(noise_mask * noise_mask_factor))

            # Color the noise by the latent
            noise_fft = torch.fft.fftn(noise.to(torch.float32), norm=fft_norm_mode)
            latent_fft = torch.fft.fftn(split_latents.to(torch.float32), norm=fft_norm_mode)
            convolve = noise_fft.mul(latent_fft)
            noise = torch.fft.ifftn(convolve, norm=fft_norm_mode).real.to(self.latents_dtype)

            # Stretch colored noise to match the image latent
            if match_mode == 0: noise = self._matchToSamplerSD(noise)
            elif match_mode == 1: noise = self._matchNorm(noise, split_latents, cf=1)
            elif match_mode == 2: noise = self._matchToSD(noise, 1)

            batch_noise.append(noise)

        noise = torch.cat(batch_noise, dim=0)

        # And mix resulting noise into the black areas of the mask
        return (init_latents * self.latent_mask) + (noise * (1 - self.latent_mask))

    def generateLatents(self):
        # Build initial latents from init_image the same as for img2img
        init_latents = self._buildInitialLatents()       
        # If strength was >=1, filled exposed areas in mask with new, shaped noise
        if self.fill_with_shaped_noise: init_latents = self._fillWithShapedNoise(init_latents)

        write_debug_latents(self.pipeline.vae, "initnoise", 0, init_latents)

        # Add the initial noise
        init_latents = self._addInitialNoise(init_latents)
        # And return
        return init_latents

    def latentStep(self, latents, i, t, steppos):
        # The type shifting here is due to note in Img2img._addInitialNoise
        init_latents_proper = self.scheduler.add_noise(self.init_latents_orig.to(self.image_noise.dtype), self.image_noise, self._getSchedulerNoiseTimestep(i, t))
        init_latents_proper = init_latents_proper.to(latents.dtype)

        iteration_mask = self.latent_blend_mask.gt(steppos).to(self.latent_blend_mask.dtype)

        write_debug_latents(self.pipeline.vae, "mask", i, self.init_latents_orig * iteration_mask)

        return (init_latents_proper * iteration_mask) + (latents * (1 - iteration_mask))       


class EnhancedRunwayInpaintMode(EnhancedInpaintMode):

    def __init__(self, do_classifier_free_guidance, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Runway inpaint unet need mask in B1HW 0K1R format
        self.inpaint_mask = 1 - self.latent_high_mask[:, [0]]
        self.masked_image_latents = self.init_latents_orig

        # Since these are passed into the unet, they need doubling
        # just like the latents are if we are doing CFG
        if do_classifier_free_guidance:
            self.inpaint_mask_cfg = torch.cat([self.inpaint_mask] * 2)
            self.masked_image_latents_cfg = torch.cat([self.masked_image_latents] * 2)

    def _fillWithShapedNoise(self, init_latents):
        return super()._fillWithShapedNoise(init_latents, noise_mode=5)

    def unet(self, latent_model_input, t, encoder_hidden_states):
        if latent_model_input.shape[0] == self.inpaint_mask.shape[0]:
            latent_model_input = torch.cat([latent_model_input, self.inpaint_mask, self.masked_image_latents], dim=1)
        else:
            latent_model_input = torch.cat([latent_model_input, self.inpaint_mask_cfg, self.masked_image_latents_cfg], dim=1)

        return self.pipeline.inpaint_unet(latent_model_input, t, encoder_hidden_states=encoder_hidden_states)

    def latentStep(self, latents, i, t, steppos):
        if False: return super().latentStep(latents, i, t, steppos)
        return latents

class UnifiedPipelinePrompt():
    def __init__(self, prompts):
        self._weighted = False
        self._prompt = self.parse_prompt(prompts)

    def check_tuples(self, list):
        for item in list:
            if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str) or not isinstance(item[1], float):
                raise ValueError(f"Expected a list of (text, weight) tuples, but got {item} of type {type(item)}")
            if item[1] != 1.0:
                self._weighted = True

    def parse_single_prompt(self, prompt):
        """Parse a single prompt - no lists allowed.
        Prompt is either a text string, or a list of (text, weight) tuples"""

        if isinstance(prompt, str):
            return [(prompt, 1.0)]
        elif isinstance(prompt, list) and isinstance(prompt[0], tuple):
            self.check_tuples(prompt)
            return prompt

        raise ValueError(f"Expected a string or a list of tuples, but got {type(prompt)}")

    def parse_prompt(self, prompts):
        try:
            return [self.parse_single_prompt(prompts)]
        except:
            if isinstance(prompts, list): return [self.parse_single_prompt(prompt) for prompt in prompts]

            raise ValueError(
                f"Expected a string, a list of strings, a list of (text, weight) tuples or "
                f"a list of a list of tuples. Got {type(prompts)} instead."
            )
                
    @property
    def batch_size(self):
        return len(self._prompt)
    
    @property
    def weighted(self):
        return self._weighted

    def as_tokens(self):
        return self._prompt

    def as_unweighted_string(self):
        return [" ".join([token[0] for token in prompt]) for prompt in self._prompt]
        

UnifiedPipelinePromptType = Union[
    str,                           # Just a single string, for a batch of 1
    List[str],                     # A list of strings, for a batch of len(prompt)
    List[Tuple[str, float]],       # A list of (part, weight) token tuples, for a batch of 1
    List[List[Tuple[str, float]]], # A list of lists of (part, weight) token tuples, for a batch of len(prompt)
    UnifiedPipelinePrompt          # A pre-parsed prompt
]

UnifiedPipelineImageType = Union[
    torch.FloatTensor, PIL.Image.Image
]

class UnifiedPipeline(DiffusionPipeline):
    r"""
    Pipeline for unified image generation using Stable Diffusion.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations.
        text_encoder ([`CLIPTextModel`]):
            Frozen text-encoder. Stable Diffusion uses the text portion of
            [CLIP](https://huggingface.co/docs/transformers/model_doc/clip#transformers.CLIPTextModel), specifically
            the [clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14) variant.
        tokenizer (`CLIPTokenizer`):
            Tokenizer of class
            [CLIPTokenizer](https://huggingface.co/docs/transformers/v4.21.0/en/model_doc/clip#transformers.CLIPTokenizer).
        unet ([`UNet2DConditionModel`]): Conditional U-Net architecture to denoise the encoded image latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents.
        safety_checker ([`StableDiffusionSafetyChecker`]):
            Classification module that estimates whether generated images could be considered offensive or harmful.
            Please, refer to the [model card](https://huggingface.co/CompVis/stable-diffusion-v1-4) for details.
        feature_extractor ([`CLIPFeatureExtractor`]):
            Model that extracts features from generated images to be used as inputs for the `safety_checker`.
    """


    
    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
        unet: UNet2DConditionModel,
        scheduler: SchedulerMixin,
        safety_checker: StableDiffusionSafetyChecker,
        feature_extractor: CLIPFeatureExtractor,
        clip_model: Optional[CLIPModel] = None,
        inpaint_unet: Optional[UNet2DConditionModel] = None,
    ):
        super().__init__()

        if hasattr(scheduler.config, "steps_offset") and scheduler.config.steps_offset != 1:
            deprecation_message = (
                f"The configuration file of this scheduler: {scheduler} is outdated. `steps_offset`"
                f" should be set to 1 instead of {scheduler.config.steps_offset}. Please make sure "
                "to update the config accordingly as leaving `steps_offset` might led to incorrect results"
                " in future versions. If you have downloaded this checkpoint from the Hugging Face Hub,"
                " it would be very nice if you could open a Pull request for the `scheduler/scheduler_config.json`"
                " file"
            )
            deprecate("steps_offset!=1", "1.0.0", deprecation_message, standard_warn=False)
            new_config = dict(scheduler.config)
            new_config["steps_offset"] = 1
            scheduler._internal_dict = FrozenDict(new_config)

        # Grafted inpaint uses an inpaint_unet from a different model than the primary model 
        # as guidance to produce a nicer inpaint that EnhancedInpaintMode otherwise can
        self._grafted_inpaint = False
        self._graft_factor = 0.8
        self._structured_diffusion = False

        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            inpaint_unet=inpaint_unet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor,
            clip_model=clip_model
        )

        self.clip_defaults = {
            "vae_cutouts": 2,
            "approx_cutouts": 2,
            "no_cutouts": False,
            "guidance_scale": 0.0,
            "gradient_threshold": 0.01
        }

        if self.clip_model is not None:
            set_requires_grad(self.text_encoder, False)
            set_requires_grad(self.clip_model, False)
            set_requires_grad(self.unet, False)
            set_requires_grad(self.vae, False)

    def set_options(self, options):
        for key, value in options.items():
            if key == "grafted_inpaint": 
                self._grafted_inpaint = bool(value)
            elif key == "graft_factor": 
                self._graft_factor = float(value)
            elif key == "xformers" and value:
                if has_xformers():
                    using_tome = bool(getattr(self.unet, 'r', False))
                    if using_tome: raise EnvironmentError(
                        'If using xformers and tome on the same pipeline, to need to enable xformers _first_'
                    )

                    if value == "reversible":
                        replace_cross_attention(target=self.unet, crossattention=MemoryEfficientCrossAttention, name="unet")
                        if self.inpaint_unet: replace_cross_attention(target=self.inpaint_unet, crossattention=MemoryEfficientCrossAttention, name="inpaint_unet")      
                    else:
                        self.unet.set_use_memory_efficient_attention_xformers(True)
                        if self.inpaint_unet: self.inpaint_unet.set_use_memory_efficient_attention_xformers(True)      
                else:
                    print("Warning: you asked for XFormers, but XFormers is not installed")
            elif key == "tome" and bool(value):
                print("Warning: ToMe isn't finished, and shouldn't be used")
                if tome_patcher:
                    tome_patcher.apply_tome(self.unet)
                    self.unet.r = int(value)
                else:
                    print("Warning: you asked for ToMe, but nonfree packages are not available")
            elif key == "structured_diffusion" and value:
                print("Warning: structured diffusion isn't finished, and shouldn't be used")
                replace_cross_attention(target=self.unet, crossattention=StructuredCrossAttention, name="unet")
                if self.inpaint_unet: replace_cross_attention(target=self.inpaint_unet, crossattention=StructuredCrossAttention, name="inpaint_unet")
                self._structured_diffusion = True
            elif key == "clip":
                for subkey, subval in value.items():
                    if subkey == "unet_grad":
                        set_requires_grad(self.unet, bool(subval))
                    elif subkey == "vae_grad":
                        set_requires_grad(self.vae, bool(subval))
                    elif subkey == "vae_cutouts":
                        self.clip_defaults["vae_cutouts"] = int(subval)
                    elif subkey == "approx_cutouts":
                        self.clip_defaults["approx_cutouts"] = int(subval)
                    elif subkey == "no_cutouts":
                        self.clip_defaults["no_cutouts"] = bool(subval)
                    elif subkey == "guidance_scale": 
                        self.clip_defaults["guidance_scale"] = float(subval)
                    elif subkey == "gradient_threshold":
                        self.clip_defaults["gradient_threshold"] = float(subval)
                    else:
                        raise ValueError(f"Unknown option {subkey}: {subval} passed as part of clip settings")
            elif key == "clip_vae_grad":
                set_requires_grad(self.vae, bool(value))
            else:
                raise ValueError(f"Unknown option {key}: {value} passed to UnifiedPipeline")

    def enable_attention_slicing(self, slice_size: Optional[Union[str, int]] = "auto"):
        r"""
        Enable sliced attention computation.

        When this option is enabled, the attention module will split the input tensor in slices, to compute attention
        in several steps. This is useful to save some memory in exchange for a small speed decrease.

        Args:
            slice_size (`str` or `int`, *optional*, defaults to `"auto"`):
                When `"auto"`, halves the input to the attention heads, so attention will be computed in two steps. If
                a number is provided, uses as many slices as `attention_head_dim // slice_size`. In this case,
                `attention_head_dim` must be a multiple of `slice_size`.
        """
        if slice_size == "auto":
            # half the attention head size is usually a good trade-off between
            # speed and memory
            slice_size = self.unet.config.attention_head_dim // 2
        self.unet.set_attention_slice(slice_size)

    def disable_attention_slicing(self):
        r"""
        Disable sliced attention computation. If `enable_attention_slicing` was previously invoked, this method will go
        back to computing attention in one step.
        """
        # set slice_size = `None` to disable `attention slicing`
        self.enable_attention_slicing(None)

    @torch.no_grad()
    def __call__(
        self,
        prompt: UnifiedPipelinePromptType,
        height: int = 512,
        width: int = 512,
        init_image: Optional[UnifiedPipelineImageType] = None,
        mask_image: Optional[UnifiedPipelineImageType] = None,
        outmask_image: Optional[UnifiedPipelineImageType] = None,
        strength: float = 0.0,
        num_inference_steps: int = 50,
        guidance_scale: float = 7.5,
        negative_prompt: Optional[UnifiedPipelinePromptType] = None,
        num_images_per_prompt: Optional[int] = 1,
        eta: Optional[float] = 0.0,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.FloatTensor] = None,
        max_embeddings_multiples: Optional[int] = 3,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        run_safety_checker: bool = True,
        callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
        callback_steps: Optional[int] = 1,
        clip_guidance_scale: Optional[float] = None,
        clip_gradient_threshold: Optional[float] = None,
        clip_prompt: Optional[Union[str, List[str]]] = None,
        vae_cutouts: Optional[int] = None,
        approx_cutouts: Optional[int] = None,
        no_cutouts: Optional[Union[str, bool]] = None,
    ):
        r"""
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]` or `List[Tuple[str, double]]` or List[List[Tuple[str, double]]]):
                The prompt or prompts to guide the image generation.
            height (`int`, *optional*, defaults to 512):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to 512):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored
                if `guidance_scale` is less than `1`).
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`torch.Generator`, *optional*):
                A [torch generator](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make generation
                deterministic.
                Alternatively, a list of torch generators, who's length must exactly match the length of the prompt, one
                per batch. This allows batch-size-idependant consistency (except where schedulers that use generators are 
                used)
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            callback (`Callable`, *optional*):
                A function that will be called every `callback_steps` steps during inference. The function will be
                called with the following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function will be called. If not specified, the callback will be
                called at every step.

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
            When returning a tuple, the first element is a list with the generated images, and the second element is a
            list of `bool`s denoting whether the corresponding generated image likely represents "not-safe-for-work"
            (nsfw) content, according to the `safety_checker`.
        """

        # Check CLIP before overwritting
        if clip_guidance_scale is not None and self.clip_model is None:
            print("Warning: CLIP guidance passed to a pipeline without a CLIP model. It will be ignored.")

        # Set defaults for clip
        if clip_guidance_scale is None: clip_guidance_scale = self.clip_defaults["guidance_scale"]
        if clip_gradient_threshold is None: clip_gradient_threshold = self.clip_defaults["gradient_threshold"]
        if vae_cutouts is None: vae_cutouts = self.clip_defaults["vae_cutouts"]
        if approx_cutouts is None: approx_cutouts = self.clip_defaults["approx_cutouts"]
        if no_cutouts is None: no_cutouts = self.clip_defaults["no_cutouts"]

        # Parse prompt and calculate batch size
        prompt = UnifiedPipelinePrompt(prompt)
        batch_size = prompt.batch_size

        # Match the negative prompt length to the batch_size
        if negative_prompt is None:
            negative_prompt = UnifiedPipelinePrompt([[("", 1.0)]] * batch_size)
        else:
            negative_prompt = UnifiedPipelinePrompt(negative_prompt)

        if batch_size != negative_prompt.batch_size:
            raise ValueError(
                f"negative_prompt has batch size {negative_prompt.batch_size}, but " 
                f"prompt has batch size {batch_size}. They need to match."
            )

        if clip_guidance_scale > 0:
            if clip_prompt is None:
                clip_prompt = prompt
            else:
                clip_prompt = UnifiedPipelinePrompt(clip_prompt)

            if batch_size != clip_prompt.batch_size:
                raise ValueError(
                    f"clip_prompt has batch size {clip_prompt.batch_size}, but " 
                    f"prompt has batch size {batch_size}. They need to match."
                )

        batch_total = batch_size * num_images_per_prompt

        # Match the generator list to the batch_total
        if isinstance(generator, list) and len(generator) != batch_total:
            raise ValueError(
                f"Generator passed as a list, but list length does not match "
                f"batch size {batch_size} * number of images per prompt {num_images_per_prompt}, i.e. {batch_total}"
            )

        if (callback_steps is None) or (
            callback_steps is not None and (not isinstance(callback_steps, int) or callback_steps <= 0)
        ):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
                f" {type(callback_steps)}."
            )

        if (mask_image != None and init_image == None):
            raise ValueError(f"Can't pass a mask without an image")

        if (outmask_image != None and init_image == None):
            raise ValueError(f"Can't pass a outmask without an image")

        # set timesteps
        self.scheduler.set_timesteps(num_inference_steps)

        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0


        # Get the latents dtype based on the text_embeddings dtype
        text_embedding_calculator = BasicTextEmbedding(self)
        text_embeddings, uncond_embeddings = text_embedding_calculator.get_embeddings(
            prompt=prompt,
            uncond_prompt=negative_prompt if do_classifier_free_guidance else None,
        )
        latents_dtype = text_embeddings.dtype

        if self._structured_diffusion:
            text_embedding_calculator = StructuredTextEmbedding(self, "align_seq")
        else:
            # AFAIK there's no scenario where just BasicTextEmbedding is better than LPWTextEmbedding
            # text_embedding_calculator = BasicTextEmbedding(self)
            text_embedding_calculator = LPWTextEmbedding(self, max_embeddings_multiples=max_embeddings_multiples)

        # get unconditional embeddings for classifier free guidance
        text_embeddings, uncond_embeddings = text_embedding_calculator.get_embeddings(
            prompt=prompt,
            uncond_prompt=negative_prompt if do_classifier_free_guidance else None,
        )

        text_embeddings = text_embedding_calculator.repeat(text_embeddings, num_images_per_prompt)

        if do_classifier_free_guidance:
            uncond_embeddings = text_embedding_calculator.repeat(uncond_embeddings, num_images_per_prompt)

            if isinstance(text_embeddings, torch.Tensor):
                text_embeddings = torch.cat([uncond_embeddings, text_embeddings])
            else:
                text_embeddings = (uncond_embeddings, text_embeddings)

        # Batch structure for 2 per prompt, 3 prompts, guided is:
        # p1u p1u p2u p2u p3u p3u p1g p1g p2g p2g p3g p3g

        if self.clip_model is not None and clip_guidance_scale > 0:
            clip_text_input = self.tokenizer(
                clip_prompt.as_unweighted_string(),
                padding="max_length",
                max_length=self.tokenizer.model_max_length,
                truncation=True,
                return_tensors="pt",
            ).input_ids.to(self.device)

            text_embeddings_clip = self.clip_model.get_text_features(clip_text_input)
            text_embeddings_clip = text_embeddings_clip / text_embeddings_clip.norm(p=2, dim=-1, keepdim=True)
            # duplicate text embeddings clip for each generation per prompt
            text_embeddings_clip = text_embeddings_clip.repeat_interleave(num_images_per_prompt, dim=0)

            noise_predictor = ClipGuidedNoisePredictor(
                pipeline=self, 
                text_embeddings=text_embeddings,
                guidance_scale=guidance_scale,
                text_embeddings_clip=text_embeddings_clip,
                clip_guidance_scale=clip_guidance_scale,
                clip_gradient_threshold=clip_gradient_threshold,
                vae_cutouts=vae_cutouts,
                approx_cutouts=approx_cutouts,
                no_cutouts=no_cutouts,
                generator=generator,
            )

        elif do_classifier_free_guidance:
            noise_predictor = GuidedNoisePredictor(
                pipeline=self, 
                text_embeddings=text_embeddings, 
                guidance_scale=guidance_scale)
        else:
            noise_predictor = NoisePredictor(
                pipeline=self, 
                text_embeddings=text_embeddings
            )

        mode_classes = []

        # Calculate operating mode based on arguments
        if mask_image != None: 
            if self.inpaint_unet is not None: 
                mode_classes.append(EnhancedRunwayInpaintMode)
                if self._grafted_inpaint:
                    mode_classes.append(EnhancedInpaintMode)
            else:
                mode_classes.append(EnhancedInpaintMode)
        elif init_image != None: 
            mode_classes.append(Img2imgMode)
        else: 
            mode_classes.append(Txt2imgMode)

        print(f"Modes: {mode_classes} with strength {strength}")

        modes = [mode_class(
            pipeline=self, 
            generator=generator,
            width=width, height=height,
            init_image=init_image, 
            mask_image=mask_image,
            latents_dtype=latents_dtype,
            batch_total=batch_total,
            num_inference_steps=num_inference_steps,
            strength=strength,
            do_classifier_free_guidance=do_classifier_free_guidance,
            noise_predictor=noise_predictor,
        ) for mode_class in mode_classes]

        # Get the initial starting point - either pure random noise, or the source image with some noise depending on mode
        # We only actually use the latents for the first mode, but UnifiedMode instances expect it to be called
        latents = [mode.generateLatents() for mode in modes][0]

        write_debug_latents(self.vae, "initial", 0, latents)

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        accepts_generator = "generator" in set(inspect.signature(self.scheduler.step).parameters.keys())
        accepts_noise_predictor = "noise_predictor" in set(inspect.signature(self.scheduler.step).parameters.keys())

        extra_step_kwargs = {}
        if accepts_eta: extra_step_kwargs["eta"] = eta
        if accepts_generator: extra_step_kwargs["generator"] = generator[0] if isinstance(generator, list) else generator

        t_start = modes[0].t_start
        timesteps_tensor = self.scheduler.timesteps[t_start:].to(self.device)

        for i, t in enumerate(self.progress_bar(timesteps_tensor)):
            t_index = t_start + i

            outputs = []
            for mode in modes:
                input_latents = latents

                # predict the noise residual
                noise_pred = noise_predictor(mode.unet, input_latents, t_index, t)
                # TODO: kind of a hack, deal with CLIP predictor returning latents too
                if isinstance(noise_pred, tuple): noise_pred, input_latents = noise_pred

                # compute the previous noisy sample x_t -> x_t-1
                if accepts_noise_predictor: extra_step_kwargs["noise_predictor"] = lambda latents, i, t, sigma = None: noise_predictor(mode.unet, latents, i, t, sigma = sigma, second=True)

                if isinstance(self.scheduler, KSchedulerMixin): 
                    output = self.scheduler.step(noise_pred, t_index, input_latents, **extra_step_kwargs).prev_sample
                else:
                    output = self.scheduler.step(noise_pred, t, input_latents, **extra_step_kwargs).prev_sample

                output = mode.latentStep(output, t_index, t, i / (timesteps_tensor.shape[0] + 1))
                outputs.append(output)

            if len(outputs) == 2:
                randmap = torch.rand(latents.shape, dtype=latents.dtype, generator=generator[0], device=generator[0].device).to(latents.device)

                # Linear blend between base and graft
                p = max(0, t/1000)
                p = p ** self._graft_factor
                latents = torch.where(randmap >= p, outputs[1], outputs[0])

            else:
                latents = outputs[0]


            write_debug_latents(self.vae, "step", i, latents)

            # call the callback, if provided
            if callback is not None and i % callback_steps == 0:
                callback(i, t, latents)

        latents = 1 / 0.18215 * latents
        image = self.vae.decode(latents).sample

        image = (image / 2 + 0.5).clamp(0, 1)

        if outmask_image != None:
            outmask = torch.cat([outmask_image] * batch_total)
            outmask = outmask[:, [0,1,2]]
            outmask = outmask.to(image.device)

            source =  torch.cat([init_image] * batch_total)
            source = source[:, [0,1,2]]
            source = source.to(image.device)

            image = source * (1-outmask) + image * outmask

        numpyImage = image.cpu().permute(0, 2, 3, 1).numpy()

        if run_safety_checker and self.safety_checker is not None:
            # run safety checker
            safety_cheker_input = self.feature_extractor(self.numpy_to_pil(numpyImage), return_tensors="pt").to(self.device)
            numpyImage, has_nsfw_concept = self.safety_checker(images=numpyImage, clip_input=safety_cheker_input.pixel_values.to(latents_dtype))
        else:
            has_nsfw_concept = [False] * numpyImage.shape[0]

        if output_type == "pil":
            image = self.numpy_to_pil(image)
        elif output_type == "tensor":
            image = torch.from_numpy(numpyImage).permute(0, 3, 1, 2)
        else:
            image = numpyImage

        if not return_dict:
            return (image, has_nsfw_concept)

        return StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept)
