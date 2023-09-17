from copy import copy, deepcopy
import os
import gc
from pprint import pprint
from PIL.Image import Image
import gradio as gr
import imageio.v3 as imageio
import ast
import numpy
import torch
import piexif
import piexif.helper
from einops import rearrange
from typing import List, Tuple
from pathlib import Path  # Added for directory manipulation
from torchvision import transforms

# Modules from your webui
from modules import scripts, images, shared, script_callbacks, hashes, sd_models, devices
from modules.devices import torch_gc, device, cpu, dtype_vae
from modules.processing import StableDiffusionProcessing, Processed, decode_latent_batch
from modules.scripts import PostprocessBatchListArgs

# From AnimateDiff extension
from scripts.logging_animatediff import logger_animatediff
from scripts import unet_injection
from scripts.unet_injection import InjectionParams
from motion_module import MotionWrapper, VanillaTemporalModule

# Modules from ldm
from ldm.modules.diffusionmodules.openaimodel import TimestepBlock, TimestepEmbedSequential
from ldm.modules.diffusionmodules.util import GroupNorm32
from ldm.modules.attention import SpatialTransformer

EXTENSION_DIRECTORY = scripts.basedir()
MODULE_NAME = "AnimateDiff"

class AnimateDiffScript(scripts.Script):
    motion_module: MotionWrapper = None

    def __init__(self):
        self.logger = logger_animatediff
        self.ui_controls = []
        self.hr = False

    def title(self):
        return MODULE_NAME

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def move_motion_module_to_cpu(self):
        self.logger.info("Moving motion module to CPU")
        if AnimateDiffScript.motion_module is not None:
            AnimateDiffScript.motion_module.to(cpu)
        torch_gc()
        gc.collect()

    def remove_motion_module(self):
        self.logger.info("Removing motion module from any memory")
        del AnimateDiffScript.motion_module
        AnimateDiffScript.motion_module = None
        torch_gc()
        gc.collect()
        
    def get_motion_modules_from_folder(self):
        return [f for f in os.listdir(shared.opts.data.get("animatediff_model_path", "") or 
                                      os.path.join(EXTENSION_DIRECTORY, "model")) if not f.lower().endswith('.gitkeep')]   
        
    def setup_ui_controls(self):
        # Setup UI controls
        with gr.Accordion('AnimateDiff', open=False):
            choices = self.get_motion_modules_from_folder()
            model = gr.Dropdown(choices=choices, label="Motion module", type="value")
            with gr.Row():
                enable = gr.Checkbox(value=False, label='Enable AnimateDiff')
                ping_pong = gr.Checkbox(value=True, label="Make a ping-pong loop")
                downscale = gr.Checkbox(value=True, label="Downscale after hires fix")
                restore_faces = gr.Checkbox(value=False, label="Restore faces")
            with gr.Row():
                video_length = gr.Slider(minimum=1, maximum=24, value=16, step=1, label="Number of frames", precision=0)
                fps = gr.Number(minimum=1, value=8, label="FPS", info= "(Frames per second)", precision=0)
            with gr.Row():
                unload = gr.Button(value="Move motion module to CPU (default if lowvram)")
                remove = gr.Button(value="Remove motion module from any memory")
                unload.click(fn=self.move_motion_module_to_cpu)
                remove.click(fn=self.remove_motion_module)
        self.ui_controls = enable, ping_pong, downscale, restore_faces, video_length, fps, model
        return self.ui_controls
        
    def make_controls_compatible_with_infotext_copy_paste(self, ui_controls = []):
        # Set up controls to be copy-pasted using infotext
        infotext_fields: List[Tuple[gr.components.IOComponent, str]] = []
        paste_field_names: List[str] = []
        for control in ui_controls:
            control_locator = get_control_locator(control.label)
            infotext_fields.append((control, control_locator))
            paste_field_names.append(control_locator)
        self.infotext_fields = infotext_fields
        self.paste_field_names = paste_field_names 
        
        return ui_controls

    def ui(self, is_img2img):
        return self.make_controls_compatible_with_infotext_copy_paste(self.setup_ui_controls())
    
    def get_unet(self, p):
        return p.sd_model.model.diffusion_model
    
    def inject_motion_module_to_unet(self, p: StableDiffusionProcessing, injection_params: InjectionParams):               
        unet = self.get_unet(p)
        motion_module = AnimateDiffScript.motion_module
        
        unet_injection.hack_groupnorm(injection_params)
        unet_injection.hack_timestep()
        
        unet_injection.inject_motion_module_to_unet(unet, motion_module, injection_params)
            
    def eject_motion_module_to_unet(self, p: StableDiffusionProcessing):
        unet = self.get_unet(p)
        
        unet_injection.eject_motion_module_from_unet(unet)
            
        unet_injection.restore_original_groupnorm()
        unet_injection.restore_original_timestep()
            
        if shared.cmd_opts.lowvram:
            self.move_motion_module_to_cpu()

    def load_motion_module_and_inject_motion_module_to_unet(
            self, p: StableDiffusionProcessing, injection_params: InjectionParams, model_name="mm_sd_v15.ckpt"):
        model_path = os.path.join(shared.opts.data.get("animatediff_model_path","") or os.path.join(script_dir, "model"), model_name)
        if not os.path.isfile(model_path):
            raise RuntimeError("Please download models manually.")
        
        if AnimateDiffScript.motion_module is None or AnimateDiffScript.motion_module.mm_type != model_name:
            if shared.opts.data.get("animatediff_check_hash", True):
                if get_model_hash(model_path, model_name) != get_expected_hash(model_name):
                    raise RuntimeError(f"{model_name} hash mismatch. You probably need to re-download the motion module.")
                
            self.logger.info(f"Loading motion module {model_name} from {model_path}")
            mm_state_dict = torch.load(model_path, map_location=device)
            AnimateDiffScript.motion_module = MotionWrapper(mm_state_dict, model_name)
            missed_keys = AnimateDiffScript.motion_module.load_state_dict(mm_state_dict)
            self.logger.warn(f"Missing keys {missed_keys}")
            AnimateDiffScript.motion_module.to(device)
        self.inject_motion_module_to_unet(p, injection_params)
            
    def get_model_hash(self, model_path, model_name):
        return hashes.sha256(model_path, f"AnimateDiff/{model_name}")

    def get_expected_hash(self, model_name):
        if model_name == "mm_sd_v14.ckpt":
            return 'aa7fd8a200a89031edd84487e2a757c5315460eca528fa70d4b3885c399bffd5'
        elif model_name == "mm_sd_v15.ckpt":
            return 'cf16ea656cb16124990c8e2c70a29c793f9841f3a2223073fac8bd89ebd9b69a'
        else:
            raise RuntimeError(f"Unsupported model filename {model_name}. Should be one of mm_sd_v14 or mm_sd_v15")
        
    def serialize_args_to_infotext(self, p: StableDiffusionProcessing):
        # Serialize Animatediff UI controls for infotext and add them to p.extra_generation_params
        # Get the control values from the script_args instead of the controls directly because they don't update for some reason
        control_params = {}   
        control_count = 0
        for control in self.ui_controls:
            control_params[control.label] = p.script_args[self.args_from + control_count]
            control_count += 1
            
        if len(control_params) > 0:
            p.extra_generation_params[MODULE_NAME] = f"{control_params}"

    def set_ddim_alpha(self, p: StableDiffusionProcessing): 
        self.logger.info(f"Setting DDIM alpha.")
        beta_start = 0.00085
        beta_end = 0.012
        betas = torch.linspace(beta_start, beta_end, p.sd_model.num_timesteps, dtype=torch.float32, device=device)
        alphas = 1.0 - betas
        alphas_cumprod = torch.cumprod(alphas, dim=0)
        alphas_cumprod_prev = torch.cat(
            (torch.tensor([1.0], dtype=torch.float32, device=device), alphas_cumprod[:-1]))
        p.sd_model.betas = betas 
        p.sd_model.alphas_cumprod = alphas_cumprod
        p.sd_model.alphas_cumprod_prev = alphas_cumprod_prev

    def before_process(
            self, p: StableDiffusionProcessing, enable_animatediff=False, 
            ping_pong=True, downscale=True, restore_faces=False,
            video_length=16, fps=8, model="mm_sd_v15_v2.ckpt"):
        if enable_animatediff:
            self.logger.info(f"AnimateDiff process start with video Max frames {video_length}, FPS {fps}, duration {video_length/fps},  motion module {model}.")
            assert video_length > 0 and fps > 0, "Video length and FPS should be positive."
            p.batch_size = video_length
            if hasattr(p, 'enable_hr'):
                self.hr = p.enable_hr
                p.enable_hr = False
            p.restore_faces = restore_faces
            
            injection_params = InjectionParams(
                video_length=video_length,
                unlimited_area_hack=False,
            )
            self.load_motion_module_and_inject_motion_module_to_unet(p, injection_params, model)
             
            self.serialize_args_to_infotext(p)
            self.set_ddim_alpha(p)
                
    def postprocess_batch_list(
            self, p: StableDiffusionProcessing, pp: PostprocessBatchListArgs, 
            enable_animatediff=False, ping_pong=True, downscale=True, 
            restore_faces=False, video_length=16, fps=8, model="mm_sd_v15_v2.ckpt", **kwargs):
        if enable_animatediff:
            p.main_prompt = p.all_prompts[0] ## Ensure the video's infotext displays correctly below the video

    def upscale(self, p: StableDiffusionProcessing, imgs: list[Image], downscale: bool):
        with sd_models.SkipWritingToConfig():
            sd_models.reload_model_weights(info=p.hr_checkpoint_info)

        phr = copy(p)
        phr.enable_hr = True
        phr.batch_size = 1
        phr.init(p.all_prompts, p.all_negative_prompts, p.all_subseeds)
        phr.setup_prompts()
        phr.parse_extra_network_prompts()
        phr.setup_conds()
        result = []
        with devices.without_autocast() if devices.unet_needs_upcast else devices.autocast():
            for idx, samples in enumerate(imgs):
                if shared.state.interrupted or shared.state.skipped:
                    return []
                phr.prompts=[p.prompts[idx]]
                phr.seeds = [p.seeds[idx]]
                phr.subseeds = [p.subseeds[idx]]
                img = transforms.ToTensor()(samples)
                sbatch = img[None, :, :, :]
                upscaled = phr.sample_hr_pass(None, sbatch, phr.seeds,
                                               phr.subseeds, phr.subseed_strength, phr.prompts)
                if upscaled:
                    upscaled = torch.clamp(upscaled[0], min=0.0, max=1.0)
                    img = transforms.ToPILImage()(upscaled)
                    if downscale:
                        img = images.resize_image(0, img, p.width, p.height, 'lanczos')
                    result.append(img)
        return result


    def save_video(self, p, res, ping_pong, downscale, video_length, fps, video_paths, output_directory, image_itr, generated_filename):
        video_list = res.images[image_itr:image_itr + video_length]
        if self.hr:
            upscaled_video_list = self.upscale(p, video_list, downscale)
            if len(upscaled_video_list):
                video_list = upscaled_video_list
        seq = images.get_next_sequence_number(output_directory, "")
        filename = f"{seq:05}-{generated_filename}"
        video_path_before_extension = f"{output_directory}/{filename}"
        video_extension = shared.opts.data.get("animatediff_file_format", "") or "gif"
        video_path = f"{video_path_before_extension}.{video_extension}"
        video_paths.append(video_path)
        video_duration = 1000 / fps
        video_use_lossless_quality = shared.opts.data.get("animatediff_use_lossless_quality", False)
        video_quality = shared.opts.data.get("animatediff_video_quality", 95)
        
        geninfo = res.infotext(p, res.index_of_first_image)
        use_geninfo = shared.opts.enable_pnginfo and geninfo is not None
        npimages = [numpy.array(v) for v in video_list]
        if ping_pong:
            npimages.extend(npimages[-2:0:-1])
        if video_extension == "gif":
            if shared.opts.data.get("animatediff_optimize_gif_palette", True):
                imageio.imwrite(
                    video_path, npimages, plugin='pyav', fps=fps, 
                    codec='gif', out_pixel_format='pal8',
                    filter_graph=(
                        {
                            "split": ("split", ""),
                            "palgen": ("palettegen", ""),
                            "paluse": ("paletteuse", ""),
                            "scale": ("scale", f"{video_list[0].width}:{video_list[0].height}")
                        },
                        [
                            ("video_in", "scale", 0, 0),
                            ("scale", "split", 0, 0),
                            ("split", "palgen", 1, 0),
                            ("split", "paluse", 0, 0),
                            ("palgen", "paluse", 0, 1),
                            ("paluse", "video_out", 0, 0),
                        ]
                    )
                )
            else:
                imageio.imwrite(video_path, npimages, duration=video_duration, loop=0)

        elif video_extension == "webp":
            if use_geninfo:
                exif_bytes = piexif.dump({
                        "Exif":{
                            piexif.ExifIFD.UserComment:piexif.helper.UserComment.dump(geninfo, encoding="unicode")}})
            imageio.imwrite(
                video_path, npimages, duration=video_duration, loop=0, 
                quality=video_quality, lossless=video_use_lossless_quality, exif=(exif_bytes if use_geninfo else b''))

    def postprocess(
            self, p: StableDiffusionProcessing, res: Processed, 
            enable_animatediff=False, ping_pong=True, downscale=True,
            restore_faces=False, video_length=16, fps=8,
            model="mm_sd_v15_v2.ckpt"):
        
        if enable_animatediff:
            self.eject_motion_module_to_unet(p)
            if shared.opts.data.get("animatediff_always_save_videos", True):
                
                video_paths = []
                self.logger.info("Merging images into video.")
                
                namegen = images.FilenameGenerator(p, res.seed, res.prompt, res.images[0])
                
                from pathlib import Path
                output_directory = shared.opts.data.get("animatediff_outdir_videos", "") or f"{p.outpath_samples}/AnimateDiff"
                if shared.opts.data.get("animatediff_save_to_subdirectory", True):
                    dirname = namegen.apply(shared.opts.data.get("animatediff_subdirectories_filename_pattern","") or 
                                            "[date]").lstrip(' ').rstrip('\\ /')
                    output_directory = os.path.join(output_directory, dirname)
                Path(output_directory).mkdir(exist_ok=True, parents=True)
                
                generated_filename = namegen.apply(shared.opts.data.get("animatediff_filename_pattern","") or 
                                                   "[seed]").lstrip(' ').rstrip('\\ /')
                
                if self.hr:
                    shared.state.job_count = len(res.images) - res.index_of_first_image + 1
                    shared.state.job_no = 0

                    shared.state.sampling_steps = p.hr_second_pass_steps or p.steps
                    shared.state.sampling_step = 0
                    shared.state.processing_has_refined_job_count = True

                    shared.total_tqdm.clear()
                    shared.total_tqdm.updateTotal(shared.state.sampling_steps * (len(res.images) - res.index_of_first_image))
                for image_itr in range(res.index_of_first_image, len(res.images), video_length):
                    self.save_video(p, res, ping_pong, downscale, video_length, fps, video_paths, output_directory, image_itr, generated_filename)
                    
                res.images = video_paths
                self.logger.info("AnimateDiff process end.")
        
def get_control_locator(control_label):
    return f"{MODULE_NAME} {control_label}"
            
def infotext_pasted(infotext, results):
    """Parse AnimateDiff infotext string and write result to `results` dict."""
    
    if not shared.opts.data.get("animatediff_copy_paste_infotext"):
        return 
    
    for k, v in results.items():
        if not k.startswith(MODULE_NAME):
            continue

        assert isinstance(v, str), f"Expect string but got {v}."
        try:
            parsed_dictionary = ast.literal_eval(v)
            logger_animatediff.debug(parsed_dictionary)
            for field, value in parsed_dictionary.items():
                logger_animatediff.debug(f"{field} and {value}")
                results[get_control_locator(field)] = value
            results.pop(MODULE_NAME)
        except Exception:
            logger_animatediff.warn(
                f"Failed to parse infotext, legacy format infotext is no longer supported:\n{v}"
            )
        break
     
script_callbacks.on_infotext_pasted(infotext_pasted)
