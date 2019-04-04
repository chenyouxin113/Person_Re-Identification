import time
import torch
import os
import random
import matplotlib.pyplot as plt
from collections import defaultdict
from args import parse_args
import torch.nn as nn
import torch.distributed as dist
from torch.utils.data import DataLoader
from utils.parse_config import parse_data_config
from utils.commons import get_yolo_layers
from utils.commons import model_info
from utils.commons import xywh2xyxy
from utils.commons import build_targets
from utils.commons import compute_loss
from model.yolov3 import Darknet
from utils.datasets import LoadImagesAndLabels
import test  # Import test.py to get mAP after each epoch


def init_seeds(seed=0):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def train(
        cfg,
        data_cfg,
        img_size=416,
        resume=False,
        epochs=270,
        batch_size=16,
        accumulate=1,
        multi_scale=False,
        freeze_backbone=False,
        num_workers=4,
        transfer=False,  # Transfer learning (train only YOLO layers)
        use_cpu=True,
        backend='nccl',
        world_size=1,
        rank=0,
        dist_url='tcp://127.0.0.1:9999'

):
    weights = 'weights' + os.sep
    latest = weights + 'latest.pt'
    best = weights + 'best.pt'

    use_gpu = torch.cuda.is_available()
    if use_cpu:
        use_gpu = False

    device = torch.device('cuda' if use_gpu else 'cpu')

    if multi_scale:
        img_size = 608  # initiate with maximum multi_scale size
        num_workers = 0  # bug https://github.com/ultralytics/yolov3/issues/174
    else:
        torch.backends.cudnn.benchmark = True  # unsuitable for multiscale

    # Configure run
    train_path = parse_data_config(data_cfg)['train']

    # Initialize model
    model = Darknet(cfg, img_size, device).to(device)

    # Optimizer
    lr0 = 0.001  # initial learning rate
    optimizer = torch.optim.SGD(model.parameters(), lr=lr0, momentum=0.9, weight_decay=0.0005)

    cutoff = -1  # backbone reaches to cutoff layer
    start_epoch = 0
    best_loss = float('inf')
    yl = get_yolo_layers(model)  # yolo layers
    nf = int(model.module_defs[yl[0] - 1]['filters'])  # yolo layer size (i.e. 255)

    if resume:  # Load previously saved model
        if transfer:  # Transfer learning
            chkpt = torch.load(weights + 'yolov3-spp.pt', map_location=device)
            model.load_state_dict({k: v for k, v in chkpt['model'].items() if v.numel() > 1 and v.shape[0] != 255},
                                  strict=False)
            for p in model.parameters():
                p.requires_grad = True if p.shape[0] == nf else False

        else:  # resume from latest.pt
            chkpt = torch.load(latest, map_location=device)  # load checkpoint
            model.load_state_dict(chkpt['model'])

        start_epoch = chkpt['epoch'] + 1
        if chkpt['optimizer'] is not None:
            optimizer.load_state_dict(chkpt['optimizer'])
            best_loss = chkpt['best_loss']
        del chkpt

    else:  # Initialize model with backbone (optional)
        if '-tiny.cfg' in cfg:
            cutoff = model.load_darknet_weights(weights + 'yolov3-tiny.conv.15')
        else:
            cutoff = model.load_darknet_weights(weights + 'darknet53.conv.74')

    # Set scheduler (reduce lr at epoch 250)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[250], gamma=0.1, last_epoch=start_epoch - 1)

    # Dataset
    dataset = LoadImagesAndLabels(train_path, img_size=img_size, augment=True)

    # Initialize distributed training
    if torch.cuda.device_count() > 1:
        dist.init_process_group(backend=backend, init_method=dist_url, world_size=world_size, rank=rank)
        model = torch.nn.parallel.DistributedDataParallel(model)
        sampler = torch.utils.data.distributed.DistributedSampler(dataset)
    else:
        sampler = None

    # Dataloader
    dataloader = DataLoader(dataset,
                            batch_size=batch_size,
                            num_workers=num_workers,
                            shuffle=False,
                            pin_memory=False,
                            collate_fn=dataset.collate_fn,
                            sampler=sampler)

    # Start training
    t = time.time()
    model_info(model)
    nB = len(dataloader)
    n_burnin = min(round(nB / 5 + 1), 1000)  # burn-in batches
    for epoch in range(start_epoch, epochs):
        model.train()
        print(('\n%8s%12s' + '%10s' * 7) % ('Epoch', 'Batch', 'xy', 'wh', 'conf', 'cls', 'total', 'nTargets', 'time'))

        # Update scheduler
        scheduler.step()

        # Freeze backbone at epoch 0, unfreeze at epoch 1
        if freeze_backbone and epoch < 2:
            for name, p in model.named_parameters():
                if int(name.split('.')[1]) < cutoff:  # if layer < 75
                    p.requires_grad = False if epoch == 0 else True

        mloss = defaultdict(float)  # mean loss
        for i, (imgs, targets, _, _) in enumerate(dataloader):
            imgs = imgs.to(device)
            targets = targets.to(device)

            nT = len(targets)
            if nT == 0:  # if no targets continue
                continue

            # Plot images with bounding boxes
            plot_images = False
            if plot_images:
                fig = plt.figure(figsize=(10, 10))
                for ip in range(len(imgs)):
                    boxes = xywh2xyxy(targets[targets[:, 0] == ip, 2:6]).numpy().T * img_size
                    plt.subplot(4, 4, ip + 1).imshow(imgs[ip].numpy().transpose(1, 2, 0))
                    plt.plot(boxes[[0, 2, 2, 0, 0]], boxes[[1, 1, 3, 3, 1]], '.-')
                    plt.axis('off')
                fig.tight_layout()
                fig.savefig('batch_%g.jpg' % i, dpi=fig.dpi)

            # SGD burn-in
            if epoch == 0 and i <= n_burnin:
                lr = lr0 * (i / n_burnin) ** 4
                for x in optimizer.param_groups:
                    x['lr'] = lr

            # Run model
            pred = model(imgs)

            # Build targets
            target_list = build_targets(model, targets)

            # Compute loss
            loss, loss_dict = compute_loss(pred, target_list)

            # Compute gradient
            loss.backward()

            # Accumulate gradient for x batches before optimizing
            if (i + 1) % accumulate == 0 or (i + 1) == nB:
                optimizer.step()
                optimizer.zero_grad()

            # Running epoch-means of tracked metrics
            for key, val in loss_dict.items():
                mloss[key] = (mloss[key] * i + val) / (i + 1)

            s = ('%8s%12s' + '%10.3g' * 7) % (
                '%g/%g' % (epoch, epochs - 1), '%g/%g' % (i, nB - 1),
                mloss['xy'], mloss['wh'], mloss['conf'], mloss['cls'],
                mloss['total'], nT, time.time() - t)
            t = time.time()
            print(s)

            # Multi-Scale training (320 - 608 pixels) every 10 batches
            if multi_scale and (i + 1) % 10 == 0:
                dataset.img_size = random.choice(range(10, 20)) * 32
                print('multi_scale img_size = %g' % dataset.img_size)

        # Update best loss
        if mloss['total'] < best_loss:
            best_loss = mloss['total']

        # Save training results
        save = True
        if save:
            # Save latest checkpoint
            chkpt = {'epoch': epoch,
                     'best_loss': best_loss,
                     'model': model.module.state_dict() if type(
                         model) is nn.parallel.DistributedDataParallel else model.state_dict(),
                     'optimizer': optimizer.state_dict()}
            torch.save(chkpt, latest)

            # Save best checkpoint
            if best_loss == mloss['total']:
                torch.save(chkpt, best)

            # Save backup every 10 epochs (optional)
            if epoch > 0 and epoch % 10 == 0:
                torch.save(chkpt, weights + 'backup%g.pt' % epoch)

            del chkpt

        # Calculate mAP
        with torch.no_grad():
            results = test.test(cfg, data_cfg, batch_size=batch_size, img_size=img_size, model=model)

        # Write epoch results
        with open('results.txt', 'a') as file:
            file.write(s + '%11.3g' * 3 % results + '\n')  # append P, R, mAP


if __name__ == '__main__':
    init_seeds()
    args = parse_args()

    train(
        args.config_path,
        args.data_path,
        img_size=args.img_size,
        resume=args.resume or args.transfer,
        transfer=args.transfer,
        epochs=args.epochs,
        batch_size=args.batch_size,
        accumulate=args.accumulate,
        multi_scale=args.multi_scale,
        num_workers=args.n_cpu,
        use_cpu=args.use_cpu
    )