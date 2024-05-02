"""
    Attack object detectors in a blackbox setting
    design blackbox loss
"""
# https://github.com/open-mmlab/mmcv#installation
import sys
import argparse
from pathlib import Path
from collections import defaultdict
import json as JSON
import random
import pdb

import datetime

import numpy as np
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '3'
import torch
from PIL import Image
from matplotlib import pyplot as plt
from tqdm import tqdm
from apatch import AngelicPatch


mmdet_root = Path('mmdetection/')
sys.path.insert(0, str(mmdet_root))
from utils_mmdet import vis_bbox, VOC_BBOX_LABEL_NAMES, COCO_BBOX_LABEL_NAMES, voc2coco, get_det, is_success, get_iou
from utils_mmdet import model_train

target_label_set = set([0, 2, 3, 9, 11])

def generate_mask(image_shape, bounding_boxes):
    mask = np.ones(image_shape, dtype=np.uint8)

    for box in bounding_boxes:
        x1, y1, x2, y2 = box
        
        mask[int(y1):int(y2), int(x1):int(x2)] = 0
    return mask

def PM_tensor_weight_balancing(im, adv, target, w, ensemble, eps, n_iters, alpha, dataset='voc', weight_balancing=True):
    """perturbation machine, balance the weights of different surrogate models
    args:
        im (tensor): original image, shape [1,3,h,w].cuda()
        adv (tensor): adversarial image
        target (numpy.ndarray): label for object detection, (xyxy, cls, conf)
        w (numpy.ndarray): ensemble weights
        ensemble (): surrogate ensemble
        eps (int): linf norm bound (0-255)
        n_iters (int): number of iterations
        alpha (flaot): step size

    returns:
        adv_list (list of Tensors): list of adversarial images for all iterations
        LOSS (dict of lists): 'ens' is the ensemble loss, and other individual surrogate losses
    """
    # prepare target label input: voc -> coco, since models are trained on coco
    bboxes_tgt = target[:,:4].astype(np.float32)
    labels_tgt = target[:,4].astype(int).copy()
    if dataset == 'voc':
        for i in range(len(labels_tgt)): 
            labels_tgt[i] = voc2coco[labels_tgt[i]]

    im_np = im.squeeze().cpu().numpy().transpose(1, 2, 0)
    adv_list = []
    pert = adv - im
    LOSS = defaultdict(list) # loss lists for different models
    for i in range(n_iters):
        pert.requires_grad = True
        loss_list = []
        loss_list_np = []
        for model in ensemble:
            loss = model.loss(im_np, pert, bboxes_tgt, labels_tgt)
            loss_list.append(loss)
            loss_list_np.append(loss.item())
            LOSS[model.model_name].append(loss.item())
        
        # if balance the weights at every iteration
        if weight_balancing:
            w_inv = 1/np.array(loss_list_np)
            w = w_inv / w_inv.sum()

        # print(f"w: {w}")
        loss_ens = sum(w[i]*loss_list[i] for i in range(len(ensemble)))
        loss_ens.backward()
        with torch.no_grad():
            pert = pert - alpha*torch.sign(pert.grad)
            pert = pert.clamp(min=-eps, max=eps)
            LOSS['ens'].append(loss_ens.item())
            
            # need to check whether needs to mask
            
            # add mask to attack only specify objection area/range
            # mask = torch.from_numpy(generate_mask(pert.shape[-2:], bboxes_tgt)).to("cuda")
            mask = torch.from_numpy(generate_mask(pert.shape[-2:], bboxes_tgt)).to(pert.device)
            pert = pert.masked_fill(mask.bool(), 0)

            adv = (im + pert).clip(0, 255)
            adv_list.append(adv)
    return adv_list, LOSS


def PM_tensor_weight_balancing_np(im_np, target, w_np, ensemble, eps, n_iters, alpha, dataset='voc', weight_balancing=True, adv_init=None):
    """perturbation machine, numpy input version
    
    """
    device = next(ensemble[0].parameters()).device
    im = torch.from_numpy(im_np).permute(2,0,1).unsqueeze(0).float().to(device)
    if adv_init is None:
        adv = torch.clone(im) # adversarial image
    else:
        adv = torch.from_numpy(adv_init).permute(2,0,1).unsqueeze(0).float().to(device)

    # w = torch.from_numpy(w_np).float().to(device)
    adv_list, LOSS = PM_tensor_weight_balancing(im, adv, target, w_np, ensemble, eps, n_iters, alpha, dataset, weight_balancing)
    adv_np = adv_list[-1].squeeze().cpu().numpy().transpose(1, 2, 0).astype(np.uint8)
    return adv_np, LOSS


def get_bb_loss(detections, target_clean, LOSS):
    """define the blackbox attack loss
        if the original object is detected, the loss is the conf score of the victim object
        otherwise, the original object disappears, the conf is below the threshold, the loss is the wb ensemble loss
    args:
        detections ():
        target_clean ():
        LOSS ():
    return:
        bb_loss (): the blackbox loss
    """
    max_iou = 0
    for items in detections:
        iou = get_iou(items, target_clean[0])
        if iou > max(max_iou, 0.3) and items[4] == target_clean[0][4]:
            max_iou = iou
            bb_loss = 1e3 + items[5] # add a large const to make sure it is larger than conf ens loss

    # if it disappears
    if max_iou < 0.3:
        bb_loss = LOSS['ens'][-1]

    return bb_loss


def save_det_to_fig(im_np, adv_np, LOSS, target_clean, all_models, im_id, im_idx, attack_goal, log_root, dataset, n_query):    
    """get the loss bb, success_list on all surrogate models, and save detections to fig
    
    args:

    returns:
        loss_bb (float): loss on the victim model
        success_list (list of 0/1s): successful for all models
    """
    fig_h = 5
    fig_w = 5
    n_all = len(all_models)
    fig, ax = plt.subplots(2,1+n_all,figsize=((1+n_all)*fig_w,2*fig_h))
    # 1st row, clean image, detection on surrogate models, detection on victim model
    # 2nd row, perturbed image, detection on surrogate models, detection on victim model
    row = 0
    ax[row,0].imshow(im_np)
    ax[row,0].set_title('clean image')
    for model_idx, model in enumerate(all_models):
        det_adv = model.det(im_np)

        indices_to_remove = np.any(det_adv[:, 4:5] == np.array(list(target_label_set)), axis=1)
        det_adv = det_adv[indices_to_remove]

        bboxes, labels, scores = det_adv[:,:4], det_adv[:,4], det_adv[:,5]
        vis_bbox(im_np, bboxes, labels, scores, ax=ax[row,model_idx+1], dataset=dataset)
        ax[row,model_idx+1].set_title(model.model_name)

    row = 1
    ax[row,0].imshow(adv_np)
    ax[row,0].set_title(f'adv image @ iter {n_query} \n {attack_goal}')
    success_list = [] # 1 for success, 0 for fail for all models
    for model_idx, model in enumerate(all_models):
        det_adv = model.det(adv_np)

        indices_to_remove = np.any(det_adv[:, 4:5] == np.array(list(target_label_set)), axis=1)
        det_adv = det_adv[indices_to_remove]

        bboxes, labels, scores = det_adv[:,:4], det_adv[:,4], det_adv[:,5]
        vis_bbox(adv_np, bboxes, labels, scores, ax=ax[row,model_idx+1], dataset=dataset)
        ax[row,model_idx+1].set_title(model.model_name)

        # check for success and get bb loss
        if model_idx == n_all-1:
            loss_bb = get_bb_loss(det_adv, target_clean, LOSS)

        # victim model is at the last index
        success_list.append(is_success(det_adv, target_clean))
    
    plt.tight_layout()
    if success_list[-1]:
        plt.savefig(log_root / f"{im_idx}_{im_id}_iter{n_query}_success.png")
    else:
        plt.savefig(log_root / f"{im_idx}_{im_id}_iter{n_query}.png")
    plt.close()

    return loss_bb, success_list
    

def main():
    parser = argparse.ArgumentParser(description="generate perturbations")
    parser.add_argument("--eps", type=int, default=50, help="perturbation level: 10,20,30,40,50")
    parser.add_argument("--iters", type=int, default=20, help="number of inner iterations: 5,6,10,20...")
    # parser.add_argument("--gpu", type=int, default=0, help="GPU ID: 0,1")
    parser.add_argument("--root", type=str, default='result', help="the folder name of result")
    parser.add_argument("--victim", type=str, default='DETR', help="victim model")
    parser.add_argument("--x", type=int, default=3, help="times alpha by x")
    parser.add_argument("--n_wb", type=int, default=1, help="number of models in the ensemble")
    parser.add_argument("--surrogate", type=str, default='YOLOv3', help="surrogate model when n_wb=1")
    # parser.add_argument("-untargeted", action='store_true', help="run untargeted attack")
    # parser.add_argument("--loss_name", type=str, default='cw', help="the name of the loss")
    parser.add_argument("--lr", type=float, default=1e-2, help="learning rate of w")
    parser.add_argument("--iterw", type=int, default=3, help="iterations of updating w")
    parser.add_argument("--dataset", type=str, default='coco', help="model dataset 'voc' or 'coco'. This will change the output range of detectors.")
    parser.add_argument("-single", action='store_true', help="only care about one obj")
    parser.add_argument("-no_balancing", action='store_true', help="do not balance weights at beginning")
    args = parser.parse_args()
    
    print(f"args.single: {args.single}")
    eps = args.eps
    n_iters = args.iters
    x_alpha = args.x
    alpha = eps / n_iters * x_alpha
    iterw = args.iterw
    n_wb = args.n_wb
    lr_w = args.lr
    dataset = args.dataset
    victim_name = args.victim

    # load surrogate models
    ensemble = []

    models_all = ['Faster R-CNN', 'YOLOv3','YOLOX', 'CO-DETR2']
    model_list = models_all[:n_wb]
    if n_wb == 1:
        model_list = [args.surrogate]
    for model_name in model_list:
        ensemble.append(model_train(model_name=model_name, dataset=dataset))

    # load victim model
    # ['RetinaNet', 'Libra', 'FoveaBox', 'FreeAnchor', 'DETR', 'Deformable']
    if victim_name == 'Libra':
        victim_name = 'Libra R-CNN'
    elif victim_name == 'Deformable':
        victim_name = 'Deformable DETR'

    model_victim = model_train(model_name=victim_name, dataset=dataset)
    all_model_names = model_list + [victim_name]
    all_models = ensemble + [model_victim]

    # create folders
    exp_name = f'BB_{n_wb}wb_linf_{eps}_iters{n_iters}_alphax{x_alpha}_victim_{victim_name}_lr{lr_w}_iterw{iterw}'
    if dataset != 'voc':
        exp_name += f'_{dataset}'
    if n_wb == 1:
        exp_name += f'_{args.surrogate}'
    if args.single:
        exp_name += '_single'
    if args.no_balancing:
        exp_name += '_noBalancing'

    current_time = datetime.datetime.now()
    formatted_time = current_time.strftime("%Y_%m_%d_%H_%M")
    exp_name += f'_{formatted_time}'
    print(f"\nExperiment: {exp_name} \n")
    result_root = Path(f"results_detection_voc/phase2_result/")
    exp_root = result_root / exp_name
    log_root = exp_root / 'logs'
    log_root.mkdir(parents=True, exist_ok=True)
    log_loss_root = exp_root / 'logs_loss'
    log_loss_root.mkdir(parents=True, exist_ok=True)
    adv_root = exp_root / 'advs'
    adv_root.mkdir(parents=True, exist_ok=True)
    target_root = exp_root / 'targets'
    target_root.mkdir(parents=True, exist_ok=True)

    test_image_ids = JSON.load(open(f"data/{dataset}_2to6_objects.json"))
    data_root = Path("/data/SalmanAsif/")
    if dataset == 'voc':
        im_root = data_root / "VOC/VOC2007/JPEGImages/"
        n_labels = 20
        label_names = VOC_BBOX_LABEL_NAMES
    else:
        im_root = data_root / "COCO/val2017/"
        n_labels = 80
        label_names = COCO_BBOX_LABEL_NAMES

    dict_k_sucess_id_v_query = {} # query counts of successful im_ids
    dict_k_valid_id_v_success_list = {} # lists of success for all mdoels for valid im_ids
    n_obj_list = []

    test_image_ids = JSON.load(open(f"data/test_phase2/output.json"))
    for im_idx, im_id in tqdm(enumerate(test_image_ids[:500])):
    # for im_idx, im_id in [(1, "000004")]:
        im_root = Path("data/test_phase2")
        im_path = im_root / f"{im_id}.jpg"
        im_np = np.array(Image.open(im_path).convert('RGB'))
        
        # get detection on clean images and determine target class
        det = model_victim.det(im_np)

        # indices_to_remove = np.any(det[:, 4:5] == np.array(list(target_label_set)), axis=1)
        # det = det[indices_to_remove]

        bboxes, labels, scores = det[:,:4], det[:,4], det[:,5]
        print(f"n_objects: {len(det)}")
        n_obj_list.append(len(det))
        if len(det) == 0: # if nothing is detected, skip this image
            adv_path = adv_root / f"{im_id}.jpg"
            adv_png = Image.fromarray(im_np.astype(np.uint8))
            adv_png.save(adv_path)
            continue
        else:
            dict_k_valid_id_v_success_list[im_id] = []

        # all_categories = set(labels.astype(int))  # all apperaing objects in the scene
        # # randomly select a victim
        # victim_idx = random.randint(0,len(det)-1)
        # victim_class = int(det[victim_idx,4])

        # # randomly select a target
        # select_n = 1 # for each victim object, randomly select 5 target objects
        # # target_pool = list(set(range(n_labels)) - all_categories)
        # target_pool = list(target_label_set - set([victim_class]))
        # target_pool = np.random.permutation(target_pool)[:select_n]

        # # for target_class in target_pool:
        # target_class = int(target_pool[0])

        target = det.copy()

        # randomly select a victim
        victim_idx = random.randint(0,len(det)-1)
        victim_class = int(det[victim_idx,4])

        # randomly select a target
        select_n = 1 # for each victim object, randomly select 5 target objects
        # target_pool = list(set(range(n_labels)) - all_categories)
        target_pool = list(target_label_set - set([victim_class]))
        target_pool = np.random.permutation(target_pool)[:select_n]

        # for target_class in target_pool:
        target_class = int(target_pool[0])

        # basic information of attack
        target[victim_idx, 4] = target_class


        # basic information of attack
        attack_goal = f"{label_names[victim_class]} to {label_names[target_class]}"
        info = f"im_idx: {im_idx}, im_id: {im_id}, victim_class: {label_names[victim_class]}, target_class: {label_names[target_class]}\n"
        print(info)
        file = open(exp_root / f'{exp_name}.txt', 'a')
        file.write(f"{info}\n\n")
        file.close()

        # target = det.copy()
        # only change one label
        # target[victim_idx, 4] = target_class
        # only keep one label
        target_clean = target[victim_idx,:][None]

        # if args.single: # only care about the target object
            # target = target_clean
        target = target_clean

        # save target to np
        np.save(target_root/f"{im_id}_target", target)

        # target = np.zeros([0,6]) # for vanishing attack        
        

        if args.no_balancing:
            print(f"no_balancing, using equal weights")
            w_inv = np.ones(n_wb) 
            w_np = np.ones(n_wb) / n_wb
        else:
            # determine the initial w, via weight balancing
            dummy_w = np.ones(n_wb)
            _, LOSS = PM_tensor_weight_balancing_np(im_np, target, dummy_w, ensemble, eps, n_iters=1, alpha=alpha, dataset=dataset)
            loss_list_np = [LOSS[name][0] for name in model_list]
            w_inv = 1 / np.array(loss_list_np)
            w_np = w_inv / w_inv.sum()
            print(f"w_np: {w_np}")


        adv_np, LOSS = PM_tensor_weight_balancing_np(im_np, target, w_np, ensemble, eps, n_iters, alpha=alpha, dataset=dataset)

        patch = np.load("patches/aware/{}/{}/patch_0.5.npy".format("frcnn", "person"))
        attack = AngelicPatch(
            model_victim,
            patch_shape=(16, 16, 3),
            learning_rate=eps,
            max_iter=12,
            batch_size=1,
            verbose=False,
            im_length=224,
        )

        patched_images, rand_n = attack.apply_multi_patch(torch.Tensor(adv_np).unsqueeze(0).cuda(), 
                                                    patch_external=torch.Tensor(patch).cuda(), 
                                                    gts_boxes=torch.Tensor(target[:,:4]).cuda(), 
                                                    corrupt_type=attack.cdict[0],
                                                    severity=3)

        n_query = 0
        loss_bb, success_list = save_det_to_fig(patched_images.squeeze(0), adv_np, LOSS, target_clean, all_models, im_id, im_idx, attack_goal, log_root, dataset, n_query)
        dict_k_valid_id_v_success_list[im_id].append(success_list)

        # save adv in folder
        # adv_path = adv_root / f"{im_id}_iter{n_query:02d}.png"
        adv_path = adv_root / f"{im_id}.jpg"
        adv_png = Image.fromarray(adv_np.astype(np.uint8))
        adv_png.save(adv_path)

if __name__ == '__main__':
    main()