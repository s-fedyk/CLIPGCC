import os
import zipfile
import scipy.io
from tqdm import tqdm
from PIL import Image
import matplotlib.pyplot as plt
from scipy.ndimage import gaussian_filter

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
import numpy as np
from torchvision import transforms

from CLIP import transform

"""
For verification.
"""
def plot_sample(image, gt_map):
    image = image.permute(1, 2, 0).cpu().numpy()
   
    # Squeeze maps to [H, W]
    gt_np = gt_map.squeeze().cpu().numpy()
    
    # Extract point coordinates: (row, col)
    gt_points = np.argwhere(gt_np > 0.5)
    
    gt_count = int(gt_np.sum())
    # Plot image with ground truth points
    plt.figure(figsize=(12, 5))
    plt.subplot(1, 2, 1)
    plt.imshow(image)
    if gt_points.size > 0:
        plt.scatter(gt_points[:, 1], gt_points[:, 0], c='green', marker='o', label='GT Points')
    plt.title(f"Ground Truth (Count = {gt_count})")
    plt.axis("off")
    plt.legend()
    
    # Plot image with predicted points
    plt.subplot(1, 2, 2)
    plt.imshow(image)
    plt.axis("off")
    plt.legend()
    
    plt.show()


def points_to_point_map(points, shape):
    """
    Converts an array of head coordinates into a binary point map.
    
    Args:
        points (np.array): Array of shape [N, 2] containing (x, y) coordinates.
        shape (tuple): (height, width) of the output map.
    Returns:
        point_map (np.array): Binary map with 1's at annotated head positions.
    """
    point_map = np.zeros(shape, dtype=np.float32)

    H, W = shape
    for point in points[0]:
        x, y = int(round(point[0])), int(round(point[1]))
        if x < W and y < H:
            point_map[y, x] = 1.0  # note: indexing is [row, col] = [y, x]
    return point_map

def load_gt_from_mat(gt_path, original_size):
    """
    Loads the .mat file and extracts the raw head coordinates,
    then converts them into a binary point map at full resolution.
    
    Args:
        gt_path (str): Path to the .mat file.
        original_size (tuple): (width, height) of the original image.
    Returns:
        point_map (np.array): Binary point map of shape (height, width).
    """
    mat = scipy.io.loadmat(gt_path)
    if "image_info" in mat:
        points = mat["image_info"][0, 0][0, 0]
    elif "annPoints" in mat:
        points = mat["annPoints"]
    else:
        raise ValueError("Ground truth format not recognized in " + gt_path)
    
    # If stored as a 3D array [1, N, 2], use the first element.
    if points.ndim == 3:
        points = points[0]
    # original_size is (width, height); convert to (height, width)
    orig_shape = (original_size[1], original_size[0])
    point_map = points_to_point_map(points, orig_shape)
    return point_map

def reassemble_from_patches(patches, original_shape, patch_size, vertical_overlap=0.5, horizontal_overlap=0.5):
    """
    Reassembles patches into original image shape with channel support.
    Handles both 2D (H, W) and 3D (C, H, W) tensors.
    """
    # Extract dimensions based on input shape
    if len(original_shape) == 3:
        # Channel-first format (C, H, W)
        C, H, W = original_shape
        channel_axis = 0
    else:
        # 2D format (H, W)
        H, W = original_shape
        C = None
        channel_axis = None

    ph, pw = patch_size

    # Calculate strides and padding
    v_stride = max(int(ph * (1 - vertical_overlap)), 1)
    h_stride = max(int(pw * (1 - horizontal_overlap)), 1)

    # Calculate required padding for spatial dimensions
    pad_h = (v_stride - (H - ph) % v_stride) % v_stride if (H - ph) % v_stride != 0 else 0
    pad_w = (h_stride - (W - pw) % h_stride) % h_stride if (W - pw) % h_stride != 0 else 0

    # Create accumulator with proper dimensions
    if C is not None:
        accumulator = torch.zeros((C, H + pad_h, W + pad_w), 
                                dtype=patches[0].dtype, 
                                device=patches[0].device)
    else:
        accumulator = torch.zeros((H + pad_h, W + pad_w),
                                dtype=patches[0].dtype,
                                device=patches[0].device)

    count_map = torch.zeros((H + pad_h, W + pad_w), 
                          dtype=torch.float32,
                          device=patches[0].device)

    patch_index = 0
    for i in range(0, (H + pad_h) - ph + 1, v_stride):
        for j in range(0, (W + pad_w) - pw + 1, h_stride):
            current_patch = patches[patch_index]
            
            if C is not None:
                # Handle 3D tensor with channels
                accumulator[:, i:i+ph, j:j+pw] += current_patch
            else:
                # Handle 2D tensor
                accumulator[i:i+ph, j:j+pw] += current_patch
                
            count_map[i:i+ph, j:j+pw] += 1
            patch_index += 1

    # Normalize by overlap count and crop padding
    reassembled = accumulator / (count_map + 1e-6)
    
    if C is not None:
        return reassembled[:, :H, :W]  # Crop to original spatial dimensions
    else:
        return reassembled[:H, :W]
def split_into_patches(arr, patch_size, vertical_overlap=0.5, horizontal_overlap=0.5):
    """
    Splits a NumPy array (an image or ground truth) into patches with vertical and horizontal overlap.
    Adds padding if necessary to ensure all pixels are covered with overlapping patches.
    
    Args:
        arr (np.array): Array of shape (H, W, C) for images or (H, W) for GT.
        patch_size (tuple): Desired patch size (patch_h, patch_w).
        vertical_overlap (float): Fraction of the patch height that overlaps with the next patch vertically.
        horizontal_overlap (float): Fraction of the patch width that overlaps with the next patch horizontally.
    Returns:
        patches (list): List of patches as NumPy arrays.
        original_shape (tuple): Original dimensions (H, W).
        padding (tuple): Amount of padding added (pad_h, pad_w).
    """
    H, W = arr.shape[:2]
    ph, pw = patch_size

    # Calculate stride sizes
    v_stride = max(int(ph * (1 - vertical_overlap)), 1)
    h_stride = max(int(pw * (1 - horizontal_overlap)), 1)

    # Calculate required padding to make (H - ph) and (W - pw) divisible by their respective strides
    pad_h = (v_stride - (H - ph) % v_stride) % v_stride if (H - ph) % v_stride != 0 else 0
    pad_w = (h_stride - (W - pw) % h_stride) % h_stride if (W - pw) % h_stride != 0 else 0

    # Apply padding to the image (symmetric padding for better edge handling)
    padded_arr = np.pad(arr, ((0, pad_h), (0, pad_w)), mode='reflect')
    H_padded, W_padded = padded_arr.shape[:2]

    # Generate patches from the padded array
    patches = []
    for i in range(0, H_padded - ph + 1, v_stride):
        for j in range(0, W_padded - pw + 1, h_stride):
            patch = padded_arr[i:i+ph, j:j+pw]
            patches.append(patch)
    
    return patches, (H, W), (pad_h, pad_w)

def preprocess(root, processed_dir, patch_size=(224,224),
               image_extensions=('.jpg', '.jpeg', '.png'),
               gt_extensions=('.mat',)):
    """
    Preprocesses the dataset by saving full images and their patches.
    """
    images_dir = os.path.join(root, "images")
    gt_dir = os.path.join(root, "ground-truth")
    
    # Create directories for full images and patches
    proc_full_dir = os.path.join(processed_dir, "full_images")
    proc_img_patch_dir = os.path.join(processed_dir, "patches", "images")
    proc_gt_patch_dir = os.path.join(processed_dir, "patches", "gt")
    proc_gt_blur_patch_dir = os.path.join(processed_dir, "patches", "gt_blur")
    
    os.makedirs(proc_full_dir, exist_ok=True)
    os.makedirs(proc_img_patch_dir, exist_ok=True)
    os.makedirs(proc_gt_patch_dir, exist_ok=True)
    os.makedirs(proc_gt_blur_patch_dir, exist_ok=True)
    
    image_files = [f for f in os.listdir(images_dir) if f.lower().endswith(image_extensions)]
    print(f"Found {len(image_files)} images.")
    
    for fname in tqdm(image_files, desc="Preprocessing"):
        image_path = os.path.join(images_dir, fname)
        base, _ = os.path.splitext(fname)
        
        # Load and save full image
        image = Image.open(image_path).convert("RGB")
        full_save_path = os.path.join(proc_full_dir, fname)
        image.save(full_save_path)
        
        # Process patches
        image_np = np.array(image)
        original_size = image.size  # (width, height)
        
        # Load GT and create maps
        gt_path = os.path.join(gt_dir, f"GT_{base}.mat")
        if not os.path.exists(gt_path):
            print(f"GT for {fname} not found. Skipping.")
            continue
        
        gt_map = load_gt_from_mat(gt_path, original_size)
        blur_gt_map = gaussian_filter(gt_map, sigma=1)
        
        # Split into patches
        image_patches, _, _ = split_into_patches(image_np, patch_size, 0.5, 0.5)        
        # Save patches
        for idx, img_patch in enumerate(image_patches):
            patch_img = Image.fromarray(img_patch)
            img_patch_path = os.path.join(proc_img_patch_dir, f"{base}_patch_{idx}.jpg")
            patch_img.save(img_patch_path)
            
        gt_path = os.path.join(proc_gt_patch_dir, f"{base}.npy")
        np.save(gt_path, gt_map)

        gt_blur_path = os.path.join(proc_gt_blur_patch_dir, f"{base}.npy")
        np.save(gt_blur_path, blur_gt_map)
            
    
    print(f"Preprocessing complete. Data saved to {processed_dir}")

class CrowdDataset(Dataset):
    def __init__(self, root, full_transform=None, patch_transform=None, gt_transform=None):
        self.root = root
        self.full_transform = full_transform
        self.patch_transform = patch_transform
        self.gt_transform = gt_transform
        
        # Directory setup
        self.full_dir = os.path.join(root, "full_images")
        self.patch_img_dir = os.path.join(root, "patches", "images")
        self.gt_dir = os.path.join(root, "patches", "gt")
        self.gt_blur_dir = os.path.join(root, "patches", "gt_blur")
        
        # Collect all full images
        self.full_images = [os.path.join(self.full_dir, f) for f in os.listdir(self.full_dir)
                            if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        
        # Validate and collect samples
        self.samples = []
        for full_path in self.full_images:
            base = os.path.splitext(os.path.basename(full_path))[0]
            
            # Collect all image patches for this image.
            img_patches = []
            idx = 0
            while True:
                img_patch_path = os.path.join(self.patch_img_dir, f"{base}_patch_{idx}.jpg")
                if os.path.exists(img_patch_path):
                    img_patches.append(img_patch_path)
                    idx += 1
                else:
                    break
            
            # GT and blurred GT are now single maps (not per-patch)
            gt_file = os.path.join(self.gt_dir, f"{base}.npy")
            gt_blur_file = os.path.join(self.gt_blur_dir, f"{base}.npy")
            
            if os.path.exists(gt_file) and os.path.exists(gt_blur_file) and len(img_patches) > 0:
                self.samples.append({
                    'full': full_path,
                    'img_patches': img_patches,
                    'gt': gt_file,
                    'gt_blur': gt_blur_file
                })
    
    def __len__(self):
        return len(self.samples)
    
    def __getitem__(self, idx):
        sample = self.samples[idx]
        
        # Load full image.
        full_img = Image.open(sample['full']).convert("RGB")
        if self.full_transform:
            full_img = self.full_transform(full_img)
        else:
            full_img = transforms.ToTensor()(full_img)
        
        # Load image patches.
        img_patches = []
        for p in sample['img_patches']:
            patch = Image.open(p).convert("RGB")
            if self.patch_transform:
                patch = self.patch_transform(patch)
            else:
                patch = transforms.ToTensor()(patch)
            img_patches.append(patch)
        img_patches = torch.stack(img_patches)
        
        # Load the ground truth map (and add a channel dimension).
        gt = np.load(sample['gt'])
        gt = torch.from_numpy(gt).float().unsqueeze(0)
        if self.gt_transform:
            gt = self.gt_transform(gt)
        
        # Load the blurred ground truth map.
        gt_blur = np.load(sample['gt_blur'])
        gt_blur = torch.from_numpy(gt_blur).float().unsqueeze(0)
        if self.gt_transform:
            gt_blur = self.gt_transform(gt_blur)

        return full_img, img_patches, gt, gt_blur
