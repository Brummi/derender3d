import math
import os
import sys
from pathlib import Path

import cv2
import matplotlib.pyplot as plt
import numpy as np
import torch
from tqdm import tqdm

from derender3d import utils

sys.path.extend(['.', '..'])
from derender3d.dataloaders import ImageDataset
from derender3d.model import Derender3D
from derender3d.utils import unsqueezer, map_fn, to, get_ball

os.system("nvidia-smi")

cp_path = Path('results') / 'models' / 'co3d' / 'checkpoint010.pth'
co3d_base = Path('datasets') / 'co3d'
cosy_base = Path('datasets') / 'cosy'
photos_base = Path('datasets') / 'photos'


category = 'hydrant'

if category != 'cosy' and category != 'photos':
    test_path = co3d_base / f'extracted_{category}' / 'imgs_cropped' / 'val'
    test_path_precompute = co3d_base / f'extracted_{category}' / 'precomputed' / 'val'
elif category == 'cosy':
    test_path = cosy_base / 'ims' / 'val'
    test_path_precompute = cosy_base / 'precomputed' / 'val'
else:
    test_path = photos_base / 'imgs_cropped' / 'val'
    test_path_precompute = None

out = Path('results') / 'images' / 'decomposition' / 'co3d' / category

gpu_id = 0

device = f'cuda:0'
if gpu_id is not None:
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
if torch.cuda.is_available():
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = True

out.mkdir(exist_ok=True, parents=True)
image_size = 256

frames = 0
rot_start = -math.pi / 4
rot_time = 8
d_start = 2
d_speed = .0 / 100
a_min = .5
a_max = .5
b_min = 1.5
b_max = 1.5

p_reconstruction = True
p_normal = True
p_depth = False
p_bump = False
p_normal_noref = False
p_diff = True
p_spec = True
p_input = True
p_albedo = True
p_ball = False
p_nr = False
p_lr_albedo = False
p_lr_normal = False

resolution = 256

category_indices = {
    'photos': list(range(26)),
    'hydrant': [5, 19, 28, 43, 65, 100, 117, 119, 143, 149, 169, 188, 202, 210, 229, 258, 315, 332, 349, 355, 373, 393, 408, 417, 436, 457, 472],
    'toybus': [11, 26, 59, 100, 134],
    'toytruck': [4, 110, 118, 315],
    'toyplane': [280, 282],
    'sandwich': [72, 120, 137, 165, 186, 210, 228, 277],
    'toilet': [1, 56, 116, 181, 261, 300],
    'bench': [2, 32],
    'chair': [17, 19, 20, 24, 26],
    'umbrella': [43, 82, 110, 150, 261],
    'cosy': list(range(40)),
    'parkingmeter': [8],
    'tv': [352, 435, 438, 439, 441, 442, 443, 445, 446, 447]
}

category_indices_figure = {
    'hydrant': [117, 332, 355],
    'toybus': [26, 134],
    'toyplane': [49],
    'chair': [306],
    'sandwich': [228],
    'cosy': [18, 31]
}


dry_run = False

indices = category_indices[category][-2:]


class DummyTrainer:
    def __init__(self):
        self.current_epoch = 0
        self.lam_flip_start_epoch = 0


def get_xy(frame):
    rotation = rot_start + frame / rot_time * math.pi * 2.
    d = d_start + frame * d_speed
    return [math.sin(rotation) * d, math.cos(rotation) * d * .5 + .5]


def save_plot(img, file_name=None, grey=False, mask=None):
    if mask is not None:
        if mask.shape[-1] != img.shape[-1]:
            mask = np.broadcast_to(np.expand_dims(mask, -1), img.shape)
        img = np.array(img)
        img[~mask] = 0
    if dry_run:
        plt.imshow(img)
        plt.title(file_name)
        plt.show()
    else:
        cv2.imwrite(file_name, cv2.cvtColor((img * 255).clip(max=255).astype(np.uint8), cv2.COLOR_RGB2BGR) if not grey else (img * 255).clip(max=255).astype(np.uint8))


def main():
    print(f'Loading dataset {category}')

    dataset = ImageDataset(str(test_path), image_size=image_size, crop=None, is_validation=True, precomputed_dir=test_path_precompute, cfgs={'min_depth': .9, 'max_depth': 1.1})

    print('Loading checkpoint')
    cp = torch.load(cp_path, map_location=device)

    print('Building model')
    model = Derender3D({
            'device': device,
            'predict_geometry': 'hr_depth',
            'image_size': 256,
            'use_gan': False,
            'autoencoder_depth': 9,
            'not_load_nets': ['netDisc'],
            'compute_loss': False,
            'if_module_params': {'spec_alpha': 'single', 'spec_strength': 'single', 'spec_alpha_max': 64, 'spec_strength_min': 0.1, 'neural_refinement': False, 'shadow': False, 'nr_spec': True, 'shadow_cap': .1, 'nr_nf': 32, 'nr_depth': 6,'nr_albedo': False}
        })

    models = [model]
    cps = [cp]

    for model, cp in zip(models, cps):
        model.trainer = DummyTrainer()
        model.load_model_state(cp)
        model.to_device(device)
        model.set_eval()

        print('Indices: ', indices)

        for index in indices:
            data_dict = dataset.__getitem__(index)
            map_fn(data_dict, unsqueezer)
            data_dict = to(data_dict, device)

            for model in models:
                data_dict_ = dict(data_dict)

                for i in tqdm(range(frames+1)):

                    light_d = torch.tensor([get_xy(i)]).to(device).repeat(1, 1)
                    lam = (i - 1) / max((frames - 1), 1)
                    light_a = (1 - lam) * a_min + lam * a_max
                    light_b = (1 - lam) * b_min + lam * b_max
                    light_d_norm = torch.cat([light_d, torch.ones_like(light_d)[:, :1]], dim=1)
                    light_d_norm = light_d_norm / torch.norm(light_d_norm, dim=1, keepdim=True)
                    light = torch.cat([torch.tensor([[light_a, light_b]]).to(device), light_d], dim=-1)
                    # light = light_d

                    if i == 0:
                        light = None

                    with torch.no_grad():
                        model.forward(data_dict_, light=light)

                    print(model.data_dict['recon_light_spec_strength'].mean().cpu())

                    recon_im = model.data_dict['recon_im'][0][0].permute(1, 2, 0).cpu().clamp(-1, 1).numpy() / 2. + .5
                    light_d = light_d.cpu()

                    if category != 'cosy' and category != 'photos':
                        mask = data_dict['foreground_mask'][0].cpu().squeeze().numpy() > .5
                    elif category == 'cosy':
                        mask = model.data_dict['lr_recon_im_mask'][0, 0].cpu().numpy() > 0
                    elif category == 'photos':
                        mask_path = test_path / 'masks' / 'val' f'{index:06d}.png'
                        if mask_path.exists():
                            mask = utils.load_array(str(mask_path))[:, :, 0] > .0
                        else:
                            mask = None
                    else:
                        mask = None

                    ball = get_ball(light_d_norm[0].cpu()).numpy()

                    if p_reconstruction:
                        save_plot(recon_im, str(out / f'{index:06d}_{i}.jpg'), False, mask=(None if i==0 else mask))
                    if p_ball:
                        save_plot(ball, str(out / f'shading_{i}.jpg'), True)

                    diff = model.data_dict['recon_diffuse_shading'][0][0, 0].detach().cpu().numpy()
                    spec = model.data_dict['recon_specular_shading'][0][0, 0].detach().cpu().numpy()
                    if p_diff:
                        save_plot(diff, str(out / f'{index:06d}_{i}_diff.jpg'), grey=True, mask=mask)
                    if p_spec:
                        save_plot(spec, str(out / f'{index:06d}_{i}_spec.jpg'), grey=True, mask=mask)
                    if p_nr:
                        nr = model.data_dict['neural_shading'][0][0, 0].detach().cpu().numpy()
                        save_plot(nr, str(out / f'{index:06d}_{i}_nr.jpg'), grey=True, mask=mask)

                    if i == 0:
                        input_im = model.data_dict['input_im'][0].permute(1, 2, 0).cpu().clamp(-1, 1).numpy() / 2. + .5
                        albedo = model.data_dict['recon_albedo'][0][0].permute(1, 2, 0).cpu().clamp(-1, 1).numpy() / 2. + .5
                        depth = (model.data_dict['recon_depth'][0][0].detach().squeeze().cpu().numpy() - model.min_depth) / (model.max_depth - model.min_depth)
                        normal = model.data_dict['recon_normal'][0][0].permute(1, 2, 0).detach().cpu().numpy() / 2 + 0.5
                        if p_input:
                            save_plot(input_im, str(out / f'{index:06d}_input.jpg'), False)
                        if p_albedo:
                            save_plot(albedo, str(out / f'{index:06d}_albedo.jpg'), False, mask=mask)
                        if p_depth:
                            save_plot(depth, str(out / f'{index:06d}_depth.jpg'), True, mask=mask)
                        if p_normal:
                            save_plot(normal, str(out / f'{index:06d}_normal.jpg'), False, mask=mask)
                        if p_bump:
                            bump = model.data_dict['recon_bump'][0].permute(1, 2, 0).detach().cpu().numpy() / 2 + 0.5
                            save_plot(bump, str(out / f'{index:06d}_bump.jpg'), False, mask=mask)
                        if p_normal_noref:
                            normal_noref = model.data_dict['recon_normal_noref'][0].permute(1, 2, 0).detach().cpu().numpy() / 2 + 0.5
                            save_plot(normal_noref, str(out / f'{index:06d}_normal_noref.jpg'), False, mask=mask)
                        if p_lr_albedo:
                            lr_albedo = model.data_dict['lr_recon_albedo'][0].permute(1, 2, 0).cpu().clamp(-1, 1).numpy() / 2. + .5
                            save_plot(lr_albedo, str(out / f'{index:06d}_lr_albedo.jpg'), False)
                        if p_lr_normal:
                            lr_normal = model.data_dict['lr_recon_normal'][0].permute(1, 2, 0).cpu().clamp(-1, 1).numpy() / 2. + .5
                            lr_normal[:, :, [0, 1]] = 1 - lr_normal[:, :, [0, 1]]
                            save_plot(lr_normal, str(out / f'{index:06d}_lr_normal.jpg'), False)


if __name__ == '__main__':
    main()