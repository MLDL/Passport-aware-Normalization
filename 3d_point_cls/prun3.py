"""
Author: Benny
Date: Nov 2019
"""
import argparse
import numpy as np
import os
import torch
import datetime
import logging
from pathlib import Path
from tqdm import tqdm
import sys
import provider
import importlib
import shutil
from pprint import pprint
from data import getData, getData2
import time
from models.pointnet_util import re_initializer_layer
from utils import  Logger,  savefig
from models.loss.sign_loss import SignLoss



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(os.path.join(ROOT_DIR, 'models'))



'''PARAMETERS'''
parser = argparse.ArgumentParser('PointNet')
parser.add_argument('--seed', type=int, default=0, help=' seed value [default: 0]')
parser.add_argument('--batch_size', type=int, default=16, help='batch size in training [default: 24]')

parser.add_argument('--gpu', type=str, default='0', help='specify gpu device [default: 0]')
parser.add_argument('--num_point', type=int, default=1024, help='Point Number [default: 1024]')
parser.add_argument('--num_class', type=int, default=40, help='Class Number [default: 40,16]')
parser.add_argument('--log_dir', type=str, default=None, help='experiment root')
parser.add_argument('--normal', action='store_true', default=False, help='Whether to use normal information [default: False]')
parser.add_argument('--optimizer', type=str, default='Adam', help='optimizer for training [default: Adam]')
parser.add_argument('--decay_rate', type=float, default=1e-4, help='decay rate [default: 1e-4]')
parser.add_argument('--learning_rate', default=0.001, type=float, help='learning rate in training [default: 0.001]')
parser.add_argument('--epoch',  default=300, type=int, help='number of epoch in training [default: 300]')
parser.add_argument('--remark', type=str, default=None, help='exp remark')

parser.add_argument('--dataset', type=str, default="modelnet", help='Point Number [default: shapenet, modelnet]')
parser.add_argument('--model', default='pointnet_cls_baseline_gn', help='model name [default: pointnet_cls_ori_bn, pointnet2_cls_ssg]')
parser.add_argument('--task', type=str, default='ours', help='exp task')
parser.add_argument('--norm', type=str, default='gn', help='type of normlization [default: BN, BNCE, GN, GNCE]')
parser.add_argument('--T1',  type=int, default=0, help='1:100; other:50')



def test(model, loader, num_class=40, ind=0):
    args = parser.parse_args()
    MODEL = importlib.import_module(args.model)
    criterion = MODEL.get_loss().cuda()
    mean_loss = []
    mean_correct = []
    class_acc = np.zeros((num_class,3))
    signloss_meter = 0
    signacc_meter = 0
    model.eval()
    for j, data in tqdm(enumerate(loader), total=len(loader)):
        points, target = data
        points = points.transpose(2, 1)
        points, target = points.cuda(), target.cuda()
        pred, trans_feat = model(points, ind=ind)
        loss = criterion(pred, target.long(), trans_feat)
        mean_loss.append(loss.item() / float(points.size()[0]))
        pred_choice = pred.data.max(1)[1]
        for cat in np.unique(target.cpu()):
            classacc = pred_choice[target==cat].eq(target[target==cat].long().data).cpu().sum()
            class_acc[cat,0]+= classacc.item()/float(points[target==cat].size()[0])
            class_acc[cat,1]+=1
        correct = pred_choice.eq(target.long().data).cpu().sum()
        mean_correct.append(correct.item()/float(points.size()[0]))

        signloss = torch.tensor(0.).cuda()
        signacc = torch.tensor(0.).cuda()
        count = 0

        if ind ==1:
            for m in model.modules():
                if isinstance(m, SignLoss):
                    signloss += m.get_loss()
                    signacc += m.get_acc()
                    count += 1
            signloss_meter += signloss.item()
            try:
                signacc_meter += signacc.item() / count
            except:
                pass

    signloss = signloss_meter / len(loader)
    signacc = signacc_meter / len(loader)

    class_acc[:,2] =  class_acc[:,0]/ class_acc[:,1]
    class_acc = np.mean(class_acc[:,2])
    instance_acc = np.mean(mean_correct)
    val_loss = np.mean(mean_loss)
    return val_loss, instance_acc, class_acc, signloss, signacc



def main():
    args = parser.parse_args()
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    np.random.seed(args.seed)

    if args.remark != None:
        args.remark = args.remark
    else:
        args.remark = args.dataset + "-" + args.task + "-" + args.norm

    if args.dataset =="shapenet":
        args.num_class=16
    else:
        args.num_class=40

    def log_string(str):
        logger.info(str)
        print(str)

    '''HYPER PARAMETER'''
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu

    '''CREATE DIR'''
    timestr = str(datetime.datetime.now().strftime('%Y-%m-%d_%H-%M'))
    if   args.task == 'baseline':
        experiment_dir_root = Path('/data-x/g12/zhangjie/3dIP/baseline')
    else:
        experiment_dir_root = Path('/data-x/g12/zhangjie/3dIP/ours')
    experiment_dir_root.mkdir(exist_ok=True)
    experiment_dir = experiment_dir_root.joinpath('pruning')
    experiment_dir.mkdir(exist_ok=True)
    experiment_dir = experiment_dir.joinpath(args.remark)
    experiment_dir.mkdir(exist_ok=True)
    checkpoints_dir = experiment_dir.joinpath('checkpoints/')
    checkpoints_dir.mkdir(exist_ok=True)
    log_dir = experiment_dir.joinpath('logs/')
    log_dir.mkdir(exist_ok=True)

    '''LOG_curve'''
    title = ''

    logger_loss = Logger(os.path.join(log_dir, 'log_loss_v3.txt'), title=title)
    logger_loss.set_names([ 'Valid Pub-Clean loss', 'Valid Pub-Trigger Loss', 'Valid Pri-Clean Loss', 'Valid Pri-Trigger Loss', ])
    logger_acc = Logger(os.path.join(log_dir, 'log_acc_v3.txt'), title=title)
    logger_acc.set_names([   'Model for Releasing', 'Model for Verification', 'Trigger', 'Signature'])

    '''LOG'''  #创建log文件
    logger = logging.getLogger("Model") #log的名字
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.FileHandler('%s/%s.txt' % (log_dir, args.model))
    file_handler.setLevel(logging.INFO) #log的最低等级
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)  #log文件名
    log_string('PARAMETER ...')
    log_string(args)

    '''DATA LOADING'''
    log_string('Load pruning test dataset ...')
    if args.dataset == "shapenet":
        testDataLoader = getData.get_dataLoader(train=False, Shapenet=True, batchsize=args.batch_size)
        triggerDataLoader = getData2.get_dataLoader(Shapenet=True, T1=args.T1, batchsize=args.batch_size)
    else:
        testDataLoader = getData.get_dataLoader(train=False, Shapenet=False, batchsize=args.batch_size)
        triggerDataLoader = getData2.get_dataLoader(Shapenet=False,T1=args.T1, batchsize=args.batch_size)


    log_string('Load finished ...')



    '''MODEL LOADING'''
    num_class = args.num_class
    MODEL = importlib.import_module(args.model)

    shutil.copy('./models/%s.py' % args.model, str(experiment_dir))
    shutil.copy('./models/pointnet_util.py', str(experiment_dir))
    shutil.copy('prun3.py', str(experiment_dir))

    classifier = MODEL.get_model(num_class, channel=3).cuda()

    # pprint(classifier)

    sd = experiment_dir_root.joinpath('classification')
    sd.mkdir(exist_ok=True)
    sd = sd.joinpath(str(args.remark))
    sd.mkdir(exist_ok=True)
    sd = sd.joinpath('checkpoints/best_model.pth')

    log_string('pre-trained model chk pth: %s'%sd)

    checkpoint = torch.load(sd)
    model_dict = checkpoint['model_state_dict']
    print('Total : {}'.format(len(model_dict)))
    print("best epoch", checkpoint['epoch'])
    classifier.load_state_dict(model_dict)
    classifier.cuda()

    p_num = get_parameter_number(classifier)
    log_string('Original trainable parameter: %s'%p_num)

    '''TESTING ORIGINAL'''
    logger.info('Test original model...')

    with torch.no_grad():

        _, test_instance_acc1, class_acc1, _, _ = test(classifier, testDataLoader, num_class=args.num_class, ind=0)
        _, instance_acc_wm, class_acc_wm ,_, _ = test(classifier, triggerDataLoader, num_class=args.num_class, ind=0)
        _, test_instance_acc2, class_acc2, signloss2, signacc2 = test(classifier, testDataLoader, num_class=args.num_class,ind=1)
        _, instance_acc_wm2, class_acc_wm2, _, _ = test(classifier, triggerDataLoader,num_class=args.num_class, ind=1)

    log_string('Test Clean Public Accuracy: %f, Class Public Accuracy: %f' % (test_instance_acc1, class_acc1))
    log_string('Test Clean Private Accuracy: %f, Class Private Accuracy: %f' % (test_instance_acc2, class_acc2))
    log_string('Test  Private Sign  Accuracy: %f' % (signacc2))
    log_string('Test Trigger Public Accuracy: %f, Trigger Class Public Accuracy: %f' % (instance_acc_wm, class_acc_wm))
    log_string('Test Trigger Private Accuracy: %f, Trigger Class Private Accuracy: %f' % (instance_acc_wm2, class_acc_wm2))

    '''PRUNING'''
    logger.info('Start testing of pruning...')

    for perc in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        time_start = datetime.datetime.now()
        classifier.load_state_dict(model_dict)
        p_num = get_parameter_number(classifier)
        log_string('Original trainable parameter: %s' % p_num)

        '''Testing pruning model'''
        logger.info('Testing pruning model--%d%%'%perc)
        pruning_net(classifier, perc)
        classifier.cuda()
        p_num = get_parameter_number(classifier)
        log_string('Pruning %02d%% -- trainable parameter: %s' % (perc, p_num))

        with torch.no_grad():

            val_loss1, test_instance_acc1, class_acc1, _, _ = test(classifier, testDataLoader, num_class=args.num_class, ind=0)
            val_loss_wm1, instance_acc_wm, class_acc_wm, _, _ = test(classifier, triggerDataLoader, num_class=args.num_class, ind=0)
            val_loss2, test_instance_acc2, class_acc2, signloss2, signacc2 = test(classifier, testDataLoader, num_class=args.num_class, ind=1)
            val_loss_wm2, instance_acc_wm2, class_acc_wm2, _, _ = test(classifier, triggerDataLoader, num_class=args.num_class, ind=1)

        log_string('Pruning %d%% Test Clean Public Accuracy: %f, Class Public Accuracy: %f' % (perc,test_instance_acc1, class_acc1))
        log_string('Pruning %d%% Test Clean Private Accuracy: %f, Class Private Accuracy: %f' % (perc,test_instance_acc2, class_acc2))
        log_string('Pruning %d%% Test Private Sign  Accuracy: %f' % (perc,signacc2))
        log_string('Pruning %d%% Test Trigger Public Accuracy: %f, Trigger Class Public Accuracy: %f' % (perc,instance_acc_wm, class_acc_wm))
        log_string('Pruning %d%% Test Trigger Private Accuracy: %f, Trigger Class Private Accuracy: %f' % (perc,instance_acc_wm2, class_acc_wm2))

        logger_loss.append([ val_loss1, val_loss_wm1, val_loss2,val_loss_wm2])
        logger_acc.append([test_instance_acc1 *100 ,  test_instance_acc2 *100,  instance_acc_wm*100, signacc2*100])
        time_end = datetime.datetime.now()
        time_span_str = str((time_end - time_start).seconds)
        log_string('Epoch time : %s S' % (time_span_str))

    logger_loss.close()
    logger_loss.plot_prun()
    savefig(os.path.join(log_dir, 'log_loss.eps'))
    logger_acc.close()
    logger_acc.plot_prun()
    acc_name = args.remark + '_prun.eps'
    savefig(os.path.join(log_dir, acc_name))

    logger.info('End of pruning...')

def get_parameter_number(net):
    total_num = sum(p.numel() for p in net.parameters())
    trainable_num = sum(p.numel() for p in net.parameters() if p.requires_grad)
    return {'Total': total_num, 'Trainable': trainable_num}


def pruning_net(model, pruning_perc):
    if pruning_perc == 0:
        return

    allweights = []
    for p in model.parameters():
        allweights += p.data.cpu().abs().numpy().flatten().tolist()

    allweights = np.array(allweights)
    threshold = np.percentile(allweights, pruning_perc)
    # for p in model.parameters():
    #     mask = p.abs() > threshold
    #     p.data.mul_(mask.float())

    for name,p in model.named_parameters():
        if  'fc' not in name :
            mask = p.abs() > threshold
            p.data.mul_(mask.float())

if __name__ == '__main__':
    main()

