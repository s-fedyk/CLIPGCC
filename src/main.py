import logging
import os
from torch.utils.tensorboard import SummaryWriter
import time
import torch
import argparse
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
import torch.optim as optim
from torch.utils.data import DataLoader
import matplotlib.pyplot as plt
import numpy as np

from CLIPGCC import CLIPGCC
from losses import CrowdCountingLoss

from CLIP.factory import create_model_from_pretrained
from datasets import CrowdDataset, preprocess

def setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    log_file = os.path.join(log_dir, f'training_{timestamp}.log')
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    return log_dir

def save_checkpoint(model, optimizer, epoch, log_dir, is_best=False):
    state = {
        'epoch': epoch,
        'state_dict': model.state_dict(),
        'optimizer': optimizer.state_dict(),
    }
    
    filename = f'checkpoint_epoch_{epoch}.pth.tar'
    if is_best:
        filename = 'best_checkpoint.pth.tar'
    
    save_path = os.path.join(log_dir, filename)
    torch.save(state, save_path)
    logging.info(f"Saved checkpoint to {save_path}")



def parse_args():
    parser = argparse.ArgumentParser(description='CLIP-Guided Crowd Counting Training')
    parser.add_argument('--epochs', type=int, default=900,
                      help='Number of training epochs')
    parser.add_argument('--batch-size', type=int, default=8,
                      help='Input batch size for training')
    parser.add_argument('--lr', type=float, default=1e-4,
                      help='Learning rate')
    parser.add_argument('--log-dir', type=str, default='experiments',
                      help='Directory to save logs and checkpoints')
    parser.add_argument('--save-interval', type=int, default=5,
                      help='Save checkpoint every N epochs')
    parser.add_argument('--eval-interval', type=int, default=5,
                      help='Run evaluation every N epochs')
    parser.add_argument('--clip-model', type=str, default='ViT-B/32',
                      choices=['ViT-B/32', 'ViT-B/16'],
                      help='CLIP model variant to use')
    parser.add_argument('--eval-path', type=str, default='ShanghaiTech/part_B/test_data',
                      help='Input evaluation directory')
    parser.add_argument('--train-path', type=str, default='ShanghaiTech/part_B/train_data',
                      help='Input train directory')


    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_dir)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load the CLIP model and associated transforms.
    clip_model, img_transforms = create_model_from_pretrained(args.clip_model, pretrained="openai", force_quick_gelu=True)
    clip_model.to(device)
    clip_model.eval()

    # Create the CLIP-guided crowd counting model.
    clipgcc_model = CLIPGCC(clip_model).to(device)
    clipgcc_model.train()

    # Train dataset
    input_train_path = f"./data/{args.train_path}"
    processed_train_path = f"./processed/train_{args.train_path}"
    if not os.path.exists(processed_train_path):
        preprocess(input_train_path, processed_train_path)

    # Training dataset
    train_dataset = CrowdDataset(root=processed_train_path, transform=img_transforms)
    dataloader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=4)

    # Eval dataset
    input_eval_path = f"./data/{args.eval_path}"
    processed_eval_path = f"./processed/eval_{args.eval_path}"
    if not os.path.exists(processed_eval_path):
        preprocess(input_eval_path, processed_eval_path)

    eval_dataset = CrowdDataset(root=processed_eval_path, transform=img_transforms)
    eval_dataloader = DataLoader(eval_dataset, batch_size=args.batch_size, shuffle=False, num_workers=4)

    loss_fn = CrowdCountingLoss()
    optimizer = optim.Adam(clipgcc_model.parameters(), lr=args.lr)
    best_eval_mae = float('inf')

    writer = SummaryWriter()

    num_epochs = args.epochs
    for epoch in range(num_epochs):
        running_loss = 0.0
        for images, gt_maps in tqdm(dataloader, desc="Epoch Progress"):
            images = images.to(device)    # [B, 3, 224, 224]
            gt_maps = gt_maps.to(device)    # [B, 1, 224, 224]

            optimizer.zero_grad()
            pred_map = clipgcc_model(images)  # [B, 1, 224, 224]
            loss = loss_fn(pred_map, gt_maps)
            loss.backward()

            optimizer.step()

            running_loss += loss.item()

        avg_loss = running_loss / len(dataloader)
        print(f"Epoch [{epoch+1}/{num_epochs}] Loss: {avg_loss:.4f}")
        writer.add_scalar('Loss/train', avg_loss, epoch)

        #if ((epoch+1) % args.save_interval == 0):
        #kj   save_checkpoint(clipgcc_model, optimizer, epoch+1, args.log_dir)

        if ((epoch+1) % args.eval_interval == 0): 
            clipgcc_model.eval()
            total_abs_error = 0.0
            total_images = 0
            with torch.no_grad():
                for images, gt_maps in eval_dataloader:
                    images = images.to(device)
                    gt_maps = gt_maps.to(device)
                    pred_map = clipgcc_model(images)
                    
                    pred_count = pred_map.sum(dim=[1,2,3])
                    gt_count = gt_maps.sum(dim=[1,2,3])

                    total_abs_error += torch.sum(torch.abs(pred_count - gt_count)).item()
                    total_images += images.size(0)

            mae = total_abs_error / total_images

            if mae < best_eval_mae:
                save_checkpoint(clipgcc_model, optimizer, epoch+1, args.log_dir, True)
                best_eval_mae = mae

            print(f"Epoch [{epoch+1}/{num_epochs}] Evaluation MAE: {mae:.2f}")
            writer.add_scalar('mae/test', mae, epoch)

            clipgcc_model.eval()
            with torch.no_grad():
                for i in range(10):
                    image, gt_map = eval_dataset[i]
                    image_tensor = image.unsqueeze(0).to(device) 
                    pred_map = clipgcc_model(image_tensor)
                    logging.info(f"Epoch {epoch+1}: Sample {i+1} predicted count: {pred_map.sum().item():.2f}, real count: {gt_map.sum().item():.2f}")
                    print(f"Epoch {epoch+1}: Sample {i+1} predicted count: {pred_map.sum().item():.2f}, real count: {gt_map.sum().item():.2f}")
            clipgcc_model.train()
        # Switch back to train mode.
        clipgcc_model.train()

