#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author: Yue Wang
@Contact: yuewangx@mit.edu
@File: model.py
@Time: 2018/10/13 6:35 PM
"""


import os
import sys
import copy
import math, random
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from sparsemax import Sparsemax
from sinkhorn import Sinkhorn
from pykeops.torch import generic_argkmin

def knn2(K=20):
    knn = generic_argkmin(
        'SqDist(x, y)',
        'a = Vi({})'.format(20),
        'x = Vi({})'.format(3),
        'y = Vj({})'.format(3),
    )
    return knn

def knn(x, k):
    inner = -2*torch.matmul(x.transpose(2, 1), x)
    xx = torch.sum(x**2, dim=1, keepdim=True)
    pairwise_distance = -xx - inner - xx.transpose(2, 1)
 
    idx = pairwise_distance.topk(k=k, dim=-1)[1]   # (batch_size, num_points, k)
    return idx

def mish(input):
    '''
    Applies the mish function element-wise:
    mish(x) = x * tanh(softplus(x)) = x * tanh(ln(1 + exp(x)))
    See additional documentation for mish class.
    '''
    return input * torch.tanh(F.softplus(input))

def fibonacci_sphere(samples=1,randomize=False):
    rnd = 1.
    if randomize:
        rnd = random.random() * samples

    points = [[0, 0, 0]]
    offset = 2./samples
    increment = math.pi * (3. - math.sqrt(5.))

    for i in range(samples-1):
        y = ((i * offset) - 1) + (offset / 2)
        r = math.sqrt(1 - pow(y,2))

        phi = ((i + rnd) % samples) * increment

        x = math.cos(phi) * r
        z = math.sin(phi) * r

        points.append([x,y,z])

    return points

def get_graph_feature(x, k=20, idx=None):
    batch_size = x.size(0)
    num_points = x.size(2)
    x = x.view(batch_size, -1, num_points)
    if idx is None:
        idx = knn(x, k=k)   # (batch_size, num_points, k)
    device = torch.device('cuda')

    idx_base = torch.arange(0, batch_size, device=device).view(-1, 1, 1)*num_points
    idx = idx + idx_base
    idx = idx.view(-1)
    _, num_dims, _ = x.size()

    x = x.transpose(2, 1).contiguous()   # (batch_size, num_points, num_dims)  -> (batch_size*num_points, num_dims) #   batch_size * num_points * k + range(0, batch_size*num_points)
    feature = x.view(batch_size*num_points, -1)[idx, :]
    feature = feature.view(batch_size, num_points, k, num_dims) 
    x = x.view(batch_size, num_points, 1, num_dims).repeat(1, 1, k, 1)
    
    feature = torch.cat((feature-x, x), dim=3).permute(0, 3, 1, 2).contiguous()
  
    return feature

class Transform_Net(nn.Module):
    def __init__(self, args):
        super(Transform_Net, self).__init__()
        self.args = args
        self.k = 3

        self.bn1 = nn.BatchNorm2d(64)
        self.bn2 = nn.BatchNorm2d(128)
        self.bn3 = nn.BatchNorm1d(1024)

        self.conv1 = nn.Sequential(nn.Conv2d(6, 64, kernel_size=1, bias=False),
                                   self.bn1,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv2 = nn.Sequential(nn.Conv2d(64, 128, kernel_size=1, bias=False),
                                   self.bn2,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv3 = nn.Sequential(nn.Conv1d(128, 1024, kernel_size=1, bias=False),
                                   self.bn3,
                                   nn.LeakyReLU(negative_slope=0.2))

        self.linear1 = nn.Linear(1024, 512, bias=False)
        self.bn3 = nn.BatchNorm1d(512)
        self.linear2 = nn.Linear(512, 256, bias=False)
        self.bn4 = nn.BatchNorm1d(256)

        self.transform = nn.Linear(256, 6)
        init.constant_(self.transform.weight, 0)
        init.eye_(self.transform.bias.view(2, 3))

    def compute_rotation_matrix_from_ortho6d(self, poses):
        x_raw = poses[:, 0:3]  #batch*3
        y_raw = poses[:, 3:6]  #batch*3

        x = F.normalize(x_raw, p=2, dim=1)  #batch*3
        z = F.normalize(torch.cross(x, y_raw), p=2, dim=1)  #batch*3
        y = torch.cross(z, x)  #batch*3
        matrix = torch.stack((x, y, z), dim=2)  #batch*3*3
        return matrix

    def forward(self, x):
        batch_size = x.size(0)

        x = self.conv1(x)                       # (batch_size, 3*2, num_points, k) -> (batch_size, 64, num_points, k)
        x = self.conv2(x)                       # (batch_size, 64, num_points, k) -> (batch_size, 128, num_points, k)
        x = x.max(dim=-1, keepdim=False)[0]     # (batch_size, 128, num_points, k) -> (batch_size, 128, num_points)

        x = self.conv3(x)                       # (batch_size, 128, num_points) -> (batch_size, 1024, num_points)
        x = x.max(dim=-1, keepdim=False)[0]     # (batch_size, 1024, num_points) -> (batch_size, 1024)

        x = F.leaky_relu(self.bn3(self.linear1(x)), negative_slope=0.2)     # (batch_size, 1024) -> (batch_size, 512)
        x = F.leaky_relu(self.bn4(self.linear2(x)), negative_slope=0.2)     # (batch_size, 512) -> (batch_size, 256)

        x = self.transform(x)                   # (batch_size, 256) -> (batch_size, 3*3)
        x = self.compute_rotation_matrix_from_ortho6d(x)
        return x

class RandLANet(nn.Module):
    def __init__(self, in_c, out_c, k, kernel_size=20, bias=True): # ,device=None):
        super(RandLANet, self).__init__()
        self.k = k
        self.kernel_size = kernel_size
        self.in_c = in_c
        self.out_c = out_c
        self.mlp = nn.Conv1d(in_c, in_c, kernel_size=1, bias=False)
        self.conv = nn.Linear(in_c,out_c,bias=bias)
        self.bn = nn.BatchNorm1d(out_c)
        self.softmax = nn.Softmax(dim=1)

    def forward(self, feature, spirals_index, permatrix):
        bsize, feats, num_pts = feature.size()
        feature = feature.permute(0, 2, 1).contiguous().view(bsize*num_pts, feats)
        
        spirals = feature[spirals_index,:].view(bsize*num_pts, self.k, feats)
        spirals = spirals.permute(0, 2, 1).contiguous()
        
        spirals = torch.sum(self.softmax(self.mlp(spirals))*spirals, dim=-1) ## This is for RandLA-Net
        spirals = spirals.view(bsize*num_pts, feats)

        out_feat = self.conv(spirals).view(bsize,num_pts,self.out_c)  
        out_feat = self.bn(out_feat.permute(0, 2, 1).contiguous())      
        return out_feat
    
class TemperatureNet(nn.Module):
    def __init__(self, args):
        super(TemperatureNet, self).__init__()
        self.temp_factor = args.temp_factor
        self.nn = nn.Sequential(nn.Conv1d(3, 64, kernel_size=1),
                                nn.BatchNorm1d(64),
                                nn.LeakyReLU(negative_slope=0.2))
        self.linear = nn.Linear(64, 1)
                                
    def forward(self, input):
        batch_size = input.shape[0]
        x = input.permute(0, 2, 1).contiguous()
        x = self.nn(x)
        x = F.adaptive_max_pool1d(x, 1).view(batch_size, -1)
        x = torch.sigmoid(self.linear(x)[:, :, None])*(0.1 - 1.0/self.temp_factor) + 1.0/self.temp_factor
        return x
    
    
class PaiIndexMatrix(nn.Module):
    def __init__(self, args, kernel_size):
        super(PaiIndexMatrix, self).__init__()
        self.k = args.k
        self.temp_factor = args.temp_factor
        self.kernel_size = kernel_size
        # self.kernels = nn.Parameter(torch.rand(3, self.kernel_size) - 0.5, requires_grad=False)
        self.kernels = nn.Parameter(torch.tensor(fibonacci_sphere(self.kernel_size)).transpose(0, 1), requires_grad=False)
        # self.A = nn.Parameter(torch.randn(3, 3))
        
        self.one_padding = nn.Parameter(torch.zeros(self.k, self.kernel_size), requires_grad=False)
        self.one_padding.data[0, 0] = 1
        self.temp_net = TemperatureNet(args)
        self.softmax = nn.Softmax(dim=1) # Sparsemax(dim=-1) #
        # self.sinkhorn = Sinkhorn(max_iter=10, is_norm=False, is_log=False)
        
        # self.mlp = nn.Conv1d(10, 16, kernel_size=1, bias=False)
        # self.mlp_out = nn.Conv1d(3, 16, kernel_size=1, bias=False)
        # self.conv = nn.Linear(16*self.kernel_size,16,bias=True)
        # self.bn = nn.BatchNorm1d(16)
        # self.reset_parameters()
    
    def topkmax(self, permatrix):
        permatrix = permatrix / (torch.sum(permatrix, dim=1, keepdim=True) + 1e-6)
        permatrix = permatrix * permatrix
        permatrix = permatrix / (torch.sum(permatrix, dim=1, keepdim=True) + 1e-6)
        permatrix = torch.where(permatrix > 0.1, permatrix, torch.full_like(permatrix, 0.)) 
        return permatrix

    # def topk2max(self, permatrix):
    #     b, nb, k = permatrix.shape
    #     idx = torch.topk(-permatrix, k=nb-3, dim=1)[1]
    #     permatrix.scatter_(1, idx, 0)
    #     permatrix = permatrix * permatrix
    #     permatrix = permatrix / (torch.sum(permatrix, dim=1, keepdim=True) + 1e-6)
    #     return permatrix

    def forward(self, x):
        bsize, feats, num_pts = x.size()
        spirals_index = knn(x, self.k)
        idx_base = torch.arange(0, bsize, device=x.device).view(-1, 1, 1)*num_pts
        spirals_index = (spirals_index + idx_base).view(-1) # bsize*num_pts*spiral_size
        spirals = x.permute(0, 2, 1).contiguous().view(bsize*num_pts, feats)
        spirals = spirals[spirals_index,:].view(bsize*num_pts, self.k, feats)
        
        # #### relative position ####
        # x_repeat = spirals[:, 0:1, :].expand_as(spirals)
        # x_relative = spirals - x_repeat
        # x_dis = torch.norm(x_relative, dim=-1, keepdim=True)
        # x_feats = torch.cat([spirals, x_repeat, x_relative, x_dis], dim=-1)
        
        x_relative = spirals - spirals[:, 0:1, :]
        ####### Euclidean distance ########
        # permatrix = - torch.norm(x_relative[:, :, None, :] - 
        #             self.kernels.transpose(0, 1)[None, None, :, :], dim=3)
        # permatrix = (permatrix - torch.min(permatrix, dim=1, keepdim=True)[0]) / \
        #             (torch.max(permatrix, dim=1, keepdim=True)[0] - torch.min(permatrix, dim=1, keepdim=True)[0])
        ######## cosine distance ##########
        # permatrix = torch.matmul(torch.matmul(x_relative, 
        #     (self.A + self.A.transpose(0, 1)) / 2), self.kernels)

        permatrix = torch.matmul(x_relative, self.kernels)
        permatrix = (permatrix + self.one_padding) #
        permatrix = torch.where(permatrix > 0, permatrix, torch.full_like(permatrix, 0.))  # permatrix[permatrix < 0] = torch.min(permatrix)*5
        permatrix = self.topkmax(permatrix)

        # temp = self.temp_net(x_relative)
        # permatrix = self.softmax(permatrix / temp)
        # permatrix = self.sinkhorn(permatrix, temp)
        
        ##### first feature #####
        # spirals = self.mlp(x_feats.permute(0, 2, 1).contiguous())
        # spirals = torch.matmul(spirals, permatrix)
        # spirals = spirals.view(bsize*num_pts, 16*self.kernel_size)
        # out_feat = self.conv(spirals).view(bsize,num_pts,16)  
        # out_feat = self.bn(out_feat.permute(0, 2, 1).contiguous() + self.mlp_out(x))  
        return spirals_index, permatrix #, F.gelu(out_feat)


class PaiIndexMatrixLSA(nn.Module):
    def __init__(self, args, kernel_size):
        super(PaiIndexMatrixLSA, self).__init__()
        self.k = args.k
        self.temp_factor = args.temp_factor
        self.kernel_size = kernel_size
        mappingsize = 64
        self.B = nn.Parameter(torch.randn(7*self.k, mappingsize) , requires_grad=False)  
        self.mlp = nn.Linear(128, 16)
        self.permatrix = nn.Parameter(torch.randn(16, kernel_size, kernel_size), requires_grad=True)
        self.permatrix.data = torch.eye(kernel_size).unsqueeze(0).expand_as(self.permatrix)
        self.softmax = Sparsemax(dim=-1) #

    def forward(self, x):
        bsize, feats, num_pts = x.size()
        spirals_index = knn(x, self.k)
        idx_base = torch.arange(0, bsize, device=x.device).view(-1, 1, 1)*num_pts
        spirals_index = (spirals_index + idx_base).view(-1) # bsize*num_pts*spiral_size
        spirals = x.permute(0, 2, 1).contiguous().view(bsize*num_pts, feats)
        spirals = spirals[spirals_index,:].view(bsize*num_pts, self.k, feats)
        
        #### relative position ####
        x_repeat = spirals[:, 0:1, :].expand_as(spirals)
        x_relative = spirals - x_repeat
        x_dis = torch.norm(x_relative, dim=-1, keepdim=True)
        x_feats = torch.cat([x_repeat, x_relative, x_dis], dim=-1).view(bsize*num_pts, -1)
        x_feats = 2.*math.pi*x_feats @ self.B
        x_feats = torch.cat([torch.sin(x_feats), torch.cos(x_feats)], dim=-1)
        x_feats = self.softmax(self.mlp(x_feats))
        permatrix = torch.einsum('bi, ikt->bkt', x_feats, self.permatrix)
        return spirals_index, permatrix #, F.gelu(out_feat)

class PaiConv(nn.Module):
    def __init__(self, in_c, out_c, k, kernel_size=20, bias=True): # ,device=None):
        super(PaiConv, self).__init__()
        self.k = k
        self.kernel_size = kernel_size
        self.in_c = in_c
        self.out_c = out_c
        # self.mlp = nn.Conv1d(7, in_c, kernel_size=1, bias=False)
        self.conv = nn.Linear(in_c*self.kernel_size,out_c,bias=bias)
        self.bn = nn.BatchNorm1d(out_c)

    def forward(self, feature, spirals_index, permatrix):
        bsize, feats, num_pts = feature.size()
        feature = feature.permute(0, 2, 1).contiguous().view(bsize*num_pts, feats)
        
        spirals = feature[spirals_index,:].view(bsize*num_pts, self.k, feats)
        spirals = spirals.permute(0, 2, 1).contiguous()
        
        # x_feat = self.mlp(x_feat.permute(0, 2, 1).contiguous())
        # spirals = torch.cat([x_feat, spirals], dim=1)
        
        spirals = torch.matmul(spirals, permatrix)
        spirals = spirals.view(bsize*num_pts, feats*self.kernel_size)

        out_feat = self.conv(spirals).view(bsize,num_pts,self.out_c)  
        out_feat = self.bn(out_feat.permute(0, 2, 1).contiguous())      
        return out_feat

class PaiConvDG(nn.Module):
    def __init__(self, in_c, out_c, k, kernel_size=20, bias=True): # ,device=None):
        super(PaiConvDG, self).__init__()
        self.k = k
        self.kernel_size = kernel_size
        self.in_c = in_c
        self.out_c = out_c
        # self.mlp = nn.Conv1d(7, in_c, kernel_size=1, bias=False)
        self.conv = nn.Conv2d(in_c*2, out_c, kernel_size=1, bias=bias)
        self.bn = nn.BatchNorm1d(out_c)

    def forward(self, feature, spirals_index, permatrix):
        bsize, feats, num_pts = feature.size()
        feature = feature.permute(0, 2, 1).contiguous().view(bsize*num_pts, feats)
        
        spirals = feature[spirals_index,:].view(bsize*num_pts, self.k, feats)
        spirals = spirals.permute(0, 2, 1).contiguous()

        spirals = torch.matmul(spirals, permatrix)
        feature = spirals.view(bsize, num_pts, self.k, -1)
        feature = self.conv(feature.permute(0, 3, 1, 2).contiguous())
        out_feat = self.bn(torch.max(feature, dim=-1)[0])
        return out_feat
    

class PaiNet(nn.Module):
    def __init__(self, args, output_channels=40):
        super(PaiNet, self).__init__()
        self.args = args
        self.k = args.k
        num_kernel = 16
        self.paiIdxMatrix = PaiIndexMatrix(args, kernel_size=num_kernel)
        self.bn5 = nn.BatchNorm1d(args.emb_dims)
        self.activation = nn.LeakyReLU(negative_slope=0.2)

        self.conv1 = PaiConv(3, 64, self.k, num_kernel)
        self.conv2 = PaiConv(64, 64, self.k, num_kernel)
        self.conv3 = PaiConv(64, 128, self.k, num_kernel)
        self.conv4 = PaiConv(128, 256, self.k, num_kernel)
        self.conv5 = nn.Sequential(nn.Conv1d(512, args.emb_dims, kernel_size=1, bias=False),
                                   self.bn5)
        self.linear1 = nn.Linear(args.emb_dims*2, 512, bias=False)
        self.bn6 = nn.BatchNorm1d(512)
        self.dp1 = nn.Dropout(p=args.dropout)
        self.linear2 = nn.Linear(512, 256)
        self.bn7 = nn.BatchNorm1d(256)
        self.dp2 = nn.Dropout(p=args.dropout)
        self.linear3 = nn.Linear(256, output_channels)
        # self.transform_net = Transform_Net(args)

    def forward(self, x):
        batch_size, feats, num_pts = x.size()

        # x0 = get_graph_feature(x, k=self.k)     # (batch_size, 3, num_points) -> (batch_size, 3*2, num_points, k)
        # t = self.transform_net(x0)              # (batch_size, 3, 3)
        # x = x.transpose(2, 1)                   # (batch_size, 3, num_points) -> (batch_size, num_points, 3)
        # x = torch.bmm(x, t)                     # (batch_size, num_points, 3) * (batch_size, 3, 3) -> (batch_size, num_points, 3)
        # x = x.transpose(2, 1) 

        spirals_index, permatrix = self.paiIdxMatrix(x) 
        
        feature = F.gelu(self.conv1(x, spirals_index, permatrix))
        x1 = feature.clone()

        feature = F.gelu(self.conv2(feature, spirals_index, permatrix))
        x2 = feature.clone()
        
        feature = F.gelu(self.conv3(feature, spirals_index, permatrix))
        x3 = feature.clone()

        feature = F.gelu(self.conv4(feature, spirals_index, permatrix))
        x4 = feature.clone()

        x = torch.cat((x1, x2, x3, x4), dim=1)
        x = F.gelu(self.conv5(x))
        x1 = F.adaptive_max_pool1d(x, 1).view(batch_size, -1)
        x2 = F.adaptive_avg_pool1d(x, 1).view(batch_size, -1)
        x = torch.cat((x1, x2), 1)

        x = F.gelu(self.bn6(self.linear1(x)))
        x = self.dp1(x)
        x = F.gelu(self.bn7(self.linear2(x)))
        x = self.dp2(x)
        x = self.linear3(x)
        return x


class DGCNN(nn.Module):
    def __init__(self, args, output_channels=40):
        super(DGCNN, self).__init__()
        self.args = args
        self.k = args.k
        
        self.bn1 = nn.BatchNorm2d(64)
        self.bn2 = nn.BatchNorm2d(64)
        self.bn3 = nn.BatchNorm2d(128)
        self.bn4 = nn.BatchNorm2d(256)
        self.bn5 = nn.BatchNorm1d(args.emb_dims)

        self.conv1 = nn.Sequential(nn.Conv2d(6, 64, kernel_size=1, bias=False),
                                   self.bn1,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv2 = nn.Sequential(nn.Conv2d(64*2, 64, kernel_size=1, bias=False),
                                   self.bn2,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv3 = nn.Sequential(nn.Conv2d(64*2, 128, kernel_size=1, bias=False),
                                   self.bn3,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv4 = nn.Sequential(nn.Conv2d(128*2, 256, kernel_size=1, bias=False),
                                   self.bn4,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.conv5 = nn.Sequential(nn.Conv1d(512, args.emb_dims, kernel_size=1, bias=False),
                                   self.bn5,
                                   nn.LeakyReLU(negative_slope=0.2))
        self.linear1 = nn.Linear(args.emb_dims*2, 512, bias=False)
        self.bn6 = nn.BatchNorm1d(512)
        self.dp1 = nn.Dropout(p=args.dropout)
        self.linear2 = nn.Linear(512, 256)
        self.bn7 = nn.BatchNorm1d(256)
        self.dp2 = nn.Dropout(p=args.dropout)
        self.linear3 = nn.Linear(256, output_channels)

    def forward(self, x):
        batch_size = x.size(0)
        x = get_graph_feature(x, k=self.k)
        x = self.conv1(x)
        x1 = x.max(dim=-1, keepdim=False)[0]

        x = get_graph_feature(x1, k=self.k)
        x = self.conv2(x)
        x2 = x.max(dim=-1, keepdim=False)[0]

        x = get_graph_feature(x2, k=self.k)
        x = self.conv3(x)
        x3 = x.max(dim=-1, keepdim=False)[0]

        x = get_graph_feature(x3, k=self.k)
        x = self.conv4(x)
        x4 = x.max(dim=-1, keepdim=False)[0]

        x = torch.cat((x1, x2, x3, x4), dim=1)

        x = self.conv5(x)
        x1 = F.adaptive_max_pool1d(x, 1).view(batch_size, -1)
        x2 = F.adaptive_avg_pool1d(x, 1).view(batch_size, -1)
        x = torch.cat((x1, x2), 1)

        x = F.leaky_relu(self.bn6(self.linear1(x)), negative_slope=0.2)
        x = self.dp1(x)
        x = F.leaky_relu(self.bn7(self.linear2(x)), negative_slope=0.2)
        x = self.dp2(x)
        x = self.linear3(x)
        return x


class PointNet(nn.Module):
    def __init__(self, args, output_channels=40):
        super(PointNet, self).__init__()
        self.args = args
        self.conv1 = nn.Conv1d(3, 64, kernel_size=1, bias=False)
        self.conv2 = nn.Conv1d(64, 64, kernel_size=1, bias=False)
        self.conv3 = nn.Conv1d(64, 64, kernel_size=1, bias=False)
        self.conv4 = nn.Conv1d(64, 128, kernel_size=1, bias=False)
        self.conv5 = nn.Conv1d(128, args.emb_dims, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm1d(64)
        self.bn2 = nn.BatchNorm1d(64)
        self.bn3 = nn.BatchNorm1d(64)
        self.bn4 = nn.BatchNorm1d(128)
        self.bn5 = nn.BatchNorm1d(args.emb_dims)

        self.linear1 = nn.Linear(args.emb_dims*2, 512, bias=False)
        self.bn6 = nn.BatchNorm1d(512)
        self.dp1 = nn.Dropout(p=args.dropout)
        self.linear2 = nn.Linear(512, 256)
        self.bn7 = nn.BatchNorm1d(256)
        self.dp2 = nn.Dropout(p=args.dropout)
        self.linear3 = nn.Linear(256, output_channels)

    def forward(self, x):
        x = F.relu(self.bn1(self.conv1(x)))
        x = F.relu(self.bn2(self.conv2(x)))
        x = F.relu(self.bn3(self.conv3(x)))
        x = F.relu(self.bn4(self.conv4(x)))
        x = F.relu(self.bn5(self.conv5(x)))
        x1 = F.adaptive_max_pool1d(x, 1).squeeze()
        x2 = F.adaptive_avg_pool1d(x, 1).squeeze()
        x = torch.cat((x1, x2), 1)

        x = F.leaky_relu(self.bn6(self.linear1(x)), negative_slope=0.2)
        x = self.dp1(x)
        x = F.leaky_relu(self.bn7(self.linear2(x)), negative_slope=0.2)
        x = self.dp2(x)
        x = self.linear3(x)
        return x