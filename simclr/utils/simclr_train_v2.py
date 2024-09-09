import torch
from torch.cuda.amp import GradScaler, autocast
import torch.nn.functional as F

from simclr.utils.config import save_config_file, save_checkpoint
from simclr.utils.evaluate import accuracy

from tqdm import tqdm
import logging
import os
import wandb

def info_nce_loss(features, args):
    labels = torch.cat([torch.arange(args.batch_size) for _ in range(args.n_views)], dim=0)
    labels = (labels.unsqueeze(0) == labels.unsqueeze(1)).float()
    labels = labels.to(args.device)

    features = F.normalize(features, dim=1)

    similarity_matrix = torch.matmul(features, features.T)
    mask = torch.eye(labels.shape[0], dtype=torch.bool).to(args.device)
    labels = labels[~mask].view(labels.shape[0], -1)
    similarity_matrix = similarity_matrix[~mask].view(similarity_matrix.shape[0], -1)

    positives = similarity_matrix[labels.bool()].view(labels.shape[0], -1)
    negatives = similarity_matrix[~labels.bool()].view(similarity_matrix.shape[0], -1)

    logits = torch.cat(tensors=[positives, negatives], dim=1)
    labels = torch.zeros(logits.shape[0], dtype=torch.long).to(args.device)

    logits = logits / args.temperature
    return logits, labels

def train_simclr(model, optimizer, scheduler, train_loader, args, criterion=None):
    scaler = GradScaler(enabled=args.fp16_precision)

    # Initialize wandb
    wandb.init(project='simclr', config=args)
    wandb.watch(model, log='all')

    logging.basicConfig(filename='training.log', level=logging.DEBUG)

    # save config file
    save_config_file('./wandb_run', args)

    n_iter = 0
    logging.info(f"Start SimCLR training for {args.train_epochs} epochs.")
    logging.info(f"Training with gpu: {args.disable_cuda}.")

    for epoch_counter in range(args.train_epochs):
        for images, _ in tqdm(train_loader):
            images = torch.cat(images, dim=0)
            images = images.to(args.device)

            with autocast(enabled=args.fp16_precision):
                features = model(images)
                logits, labels = info_nce_loss(features=features, args=args)
                loss = criterion(logits, labels)

            optimizer.zero_grad()
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            if n_iter % args.log_every_n_steps == 0:
                top1, top5 = accuracy(logits, labels, topk=(1, 5))
                
                # Log metrics to wandb
                wandb.log({
                    'loss': loss.item(),
                    'acc/top1': top1[0],
                    'acc/top5': top5[0],
                    'learning_rate': scheduler.get_lr()[0],
                    'global_step': n_iter
                })

            n_iter += 1

        # warmup for the first 10 epochs
        if epoch_counter >= 10:
            scheduler.step()
        logging.debug(f"Epoch: {epoch_counter}\tLoss: {loss}\tTop1 accuracy: {top1[0]}")

    logging.info("Training has finished.")
    # save model checkpoints
    checkpoint_name = 'checkpoint_{:04d}.pth.tar'.format(args.train_epochs)
    save_checkpoint(state={'epoch': args.train_epochs,
                           'arch': args.arch,
                           'state_dict': model.state_dict(),
                           'optimizer': optimizer.state_dict(),
                           },
                    is_best=False,
                    filename=checkpoint_name)
    logging.info(f"Model checkpoint and metadata has been saved.")

    # Save model checkpoint to wandb
    wandb.save(checkpoint_name)