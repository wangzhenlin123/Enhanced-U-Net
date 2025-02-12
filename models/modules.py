import torch
from torch import nn
from torch.nn import functional as F
from einops import rearrange, repeat
import math



def scaled_dot_product(q, k, v, mask=None):
    d_k = q.size()[-1]
    attn_logits = torch.matmul(q, k.transpose(-2, -1))
    attn_logits = attn_logits / math.sqrt(d_k)
    if mask is not None:
        attn_logits = attn_logits.masked_fill(mask == 0, -9e15)
    attention = F.softmax(attn_logits, dim=-1)
    values = torch.matmul(attention, v)
    return values, attention

""" Fusion Module"""


class ASM(nn.Module):
    def __init__(self, in_channels, all_channels):
        super(ASM, self).__init__()
        self.non_local = NonLocalBlock(in_channels)

    def forward(self, lc, fuse, gc):
        fuse = self.non_local(fuse)
        fuse = torch.cat([lc, fuse, gc], dim=1)

        return fuse


"""
Squeeze and Excitation Layer

https://arxiv.org/abs/1709.01507

"""


class SELayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(SELayer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel // reduction, channel, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1, 1)
        return x * y.expand_as(x)


"""
Non Local Block

https://arxiv.org/abs/1711.07971
"""


class NonLocalBlock(nn.Module):
    def __init__(self, in_channels, inter_channels=None, sub_sample=True, bn_layer=True):
        super(NonLocalBlock, self).__init__()

        self.sub_sample = sub_sample

        self.in_channels = in_channels
        self.inter_channels = inter_channels

        if self.inter_channels is None:
            self.inter_channels = in_channels // 2
            if self.inter_channels == 0:
                self.inter_channels = 1

        self.g = nn.Conv2d(in_channels=self.in_channels, out_channels=self.inter_channels,
                           kernel_size=1, stride=1, padding=0)

        if bn_layer:
            self.W = nn.Sequential(
                nn.Conv2d(in_channels=self.inter_channels, out_channels=self.in_channels,
                          kernel_size=1, stride=1, padding=0),
                nn.BatchNorm2d(self.in_channels)
            )
            nn.init.constant_(self.W[1].weight, 0)
            nn.init.constant_(self.W[1].bias, 0)
        else:
            self.W = nn.Conv2d(in_channels=self.inter_channels, out_channels=self.in_channels,
                               kernel_size=1, stride=1, padding=0)
            nn.init.constant_(self.W.weight, 0)
            nn.init.constant_(self.W.bias, 0)

        self.theta = nn.Conv2d(in_channels=self.in_channels, out_channels=self.inter_channels,
                               kernel_size=1, stride=1, padding=0)
        self.phi = nn.Conv2d(in_channels=self.in_channels, out_channels=self.inter_channels,
                             kernel_size=1, stride=1, padding=0)

        if sub_sample:
            self.g = nn.Sequential(self.g, nn.MaxPool2d(kernel_size=(2, 2)))
            self.phi = nn.Sequential(self.phi, nn.MaxPool2d(kernel_size=(2, 2)))

    def forward(self, x):

        batch_size = x.size(0)

        g_x = self.g(x).view(batch_size, self.inter_channels, -1)
        g_x = g_x.permute(0, 2, 1)

        theta_x = self.theta(x).view(batch_size, self.inter_channels, -1)
        theta_x = theta_x.permute(0, 2, 1)
        phi_x = self.phi(x).view(batch_size, self.inter_channels, -1)
        f = torch.matmul(theta_x, phi_x)
        f_div_C = F.softmax(f, dim=-1)

        y = torch.matmul(f_div_C, g_x)
        y = y.permute(0, 2, 1).contiguous()
        y = y.view(batch_size, self.inter_channels, *x.size()[2:])
        W_y = self.W(y)
        z = W_y + x

        return z


#AGCM Module
class CrossNonLocalBlock(nn.Module):
    def __init__(self, in_channels_source,in_channels_target, inter_channels, sub_sample=False, bn_layer=True):
        super(CrossNonLocalBlock, self).__init__()

        self.sub_sample = sub_sample

        self.in_channels_source = in_channels_source
        self.in_channels_target = in_channels_target
        self.inter_channels = inter_channels

        """
        if self.inter_channels is None:
            self.inter_channels = in_channels // 2
            if self.inter_channels == 0:
                self.inter_channels = 1
        """
        self.g = nn.Conv2d(in_channels=self.in_channels_source, out_channels=self.inter_channels,
                           kernel_size=1, stride=1, padding=0)
        self.theta = nn.Conv2d(in_channels=self.in_channels_source, out_channels=self.inter_channels,
                               kernel_size=1, stride=1, padding=0)
        self.phi = nn.Conv2d(in_channels=self.in_channels_target, out_channels=self.inter_channels,
                             kernel_size=1, stride=1, padding=0)

        if bn_layer:
            self.W = nn.Sequential(
                nn.Conv2d(in_channels=self.inter_channels, out_channels=self.in_channels_target,
                          kernel_size=1, stride=1, padding=0),
                nn.BatchNorm2d(self.in_channels_target)
            )
            nn.init.constant_(self.W[1].weight, 0)
            nn.init.constant_(self.W[1].bias, 0)

        if sub_sample:
            self.g = nn.Sequential(self.g, nn.MaxPool2d(kernel_size=(2, 2)))
            self.phi = nn.Sequential(self.phi, nn.MaxPool2d(kernel_size=(2, 2)))

    def forward(self,x,l):

        batch_size = x.size(0)
        g_x = self.g(x).view(batch_size, self.inter_channels, -1)
        g_x = g_x.permute(0, 2, 1) #source
        theta_x1 = self.theta(x)
        theta_x = self.theta(x).view(batch_size, self.inter_channels, -1)
        theta_x = theta_x.permute(0, 2, 1) #source
        phi_x = self.phi(l).view(batch_size, self.inter_channels, -1) #target
        f = torch.matmul(theta_x, phi_x)
        f_div_C = F.softmax(f, dim=-1)
        f_div_C = f_div_C.permute(0,2,1)
        y = torch.matmul(f_div_C, g_x)
        y = y.permute(0, 2, 1).contiguous()
        y = y.view(batch_size, self.inter_channels, *l.size()[2:])
        W_y = self.W(y)
        z = W_y + l

        return z



#SFEM module
class NonLocalBlock_PatchWise(nn.Module):

    def __init__(self, in_channel, inter_channel, patch_factor):
        super(NonLocalBlock_PatchWise, self).__init__()
        "Embedding dimension must be 0 modulo number of heads."
        self.in_channel = in_channel
        self.patch_factor = patch_factor
        self.patch_width = int(8/self.patch_factor)
        self.patch_height = int(8/self.patch_factor)
        self.stride_width = int(8/self.patch_factor)
        self.stride_height = int(8/self.patch_factor)
        self.unfold = nn.Unfold(kernel_size=(self.patch_width, self.patch_height), stride=(self.stride_width, self.stride_height))


        self.adp = nn.AdaptiveAvgPool2d(8)
        self.bottleneck = nn.Conv2d(64,inter_channel,kernel_size=(1,1))
        self.non_block =  NonLocalBlock(self.in_channel)
        self.adp_post = nn.AdaptiveAvgPool2d((8,8))


    def forward(self, x):
        batch_size = x.size(0)
        x_up = self.adp(x)
        x_up = self.unfold(x)
        batch_size,p_dim,p_size = x_up.size()
        x_up = x_up.view(batch_size,-1,self.in_channel,p_size)
        final_output = torch.tensor([]).cuda()
        index = torch.range(0,p_size,1,dtype=torch.int64).cuda()
        for i in range(int(p_size)):
            divide = torch.index_select(x_up, 3, index[i])
            divide = divide.view(batch_size,-1,self.in_channel)
            patch_width = int(divide.size(1) ** 0.5)
            divide = divide.reshape(batch_size,self.in_channel,patch_width,patch_width) # tensor to operate on
            attn = self.non_block(divide)
            output = attn.view(batch_size,-1,self.in_channel,1)
            final_output = torch.cat((final_output,output),dim=3)



        final_output = final_output.view(batch_size, self.in_channel, 8,8)


        return final_output


class GCM_up(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(GCM_up, self).__init__()
        self.adp = nn.AdaptiveAvgPool2d((8,8))
        self.patch1 = NonLocalBlock_PatchWise(in_channels,out_channels,2)
        self.patch2 = NonLocalBlock_PatchWise(in_channels,out_channels,4)
        self.patch3 = NonLocalBlock(256,64)
        self.fuse = SELayer(3*256)
        self.conv = nn.Conv2d(3*256, out_channels, 1, 1)
        self.relu = nn.ReLU(inplace=True)


    def forward(self, x):

        b,c,h,w = x.size()
        x = self.adp(x)
        patch1 = self.patch1(x)
        patch2 = self.patch2(x)
        patch3 = self.patch3(x)
        global_cat = torch.cat((patch1, patch2, patch3), dim=1)
        fuse = self.relu(self.conv(self.fuse(global_cat)))
        adp_post = nn.AdaptiveAvgPool2d((h,w))
        fuse = adp_post(fuse)
        return fuse
