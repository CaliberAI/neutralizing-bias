from pytorch_pretrained_bert.modeling import PreTrainedBertModel, BertModel, BertSelfAttention
import pytorch_pretrained_bert.modeling as modeling
import torch
import torch.nn as nn
import numpy as np
import ops
import copy

import tagging_features
from tagging_args import ARGS

CUDA = (torch.cuda.device_count() > 0)


class BertForMultitask(PreTrainedBertModel):

    def __init__(self, config, cls_num_labels=2, tok_num_labels=2, tok2id=None):
        super(BertForMultitask, self).__init__(config)
        self.bert = BertModel(config)

        self.cls_dropout = nn.Dropout(config.hidden_dropout_prob)
        self.cls_classifier = nn.Linear(config.hidden_size, cls_num_labels)
        
        self.tok_dropout = nn.Dropout(config.hidden_dropout_prob)
        self.tok_classifier = nn.Linear(config.hidden_size, tok_num_labels)
        
        self.apply(self.init_bert_weights)


    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None, rel_ids=None, pos_ids=None):
        sequence_output, pooled_output, attn_maps = self.bert(
            input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)

        cls_logits = self.cls_classifier(pooled_output)
        cls_logits = self.cls_dropout(cls_logits)

        # NOTE -- dropout is after proj, which is non-standard
        #      -- switch back if nessicary!
        tok_logits = self.tok_classifier(sequence_output)
        tok_logits = self.tok_dropout(tok_logits)

        return cls_logits, tok_logits




class ConcatCombine(nn.Module):
    def __init__(self, in_size1, in_size2, out_size, layers, dropout_prob, small=False):
        super(ConcatCombine, self).__init__()
        if layers == 1:
            self.out = nn.Sequential(
                nn.Linear(in_size1 + in_size2, out_size),
                nn.Dropout(dropout_prob))
        elif layers == 2:
            waist_size = min(in_size1, in_size2) if small else max(in_size1, in_size2)
            self.out = nn.Sequential(
                nn.Linear(in_size1 + in_size2, waist_size),
                nn.Dropout(dropout_prob),
                nn.Linear(waist_size, out_size),
                nn.Dropout(dropout_prob))
        # manually set cuda because module doesn't see these combiners for bottom 
        if CUDA:
            self.out = self.out.cuda()

    def forward(self, in1, in2):
        return self.out(torch.cat((in1, in2), dim=-1))

class AddCombine(nn.Module):
    def __init__(self, feat_dim, hidden_dim, layers, dropout_prob, small=False, out_dim=-1):
        super(AddCombine, self).__init__()
        
        if layers == 1:
            self.expand = nn.Sequential(
                nn.Linear(feat_dim, hidden_dim),
                nn.Dropout(dropout_prob))
        else:
            waist_size = min(feat_dim, hidden_dim) if small else max(feat_dim, hidden_dim)
            self.expand = nn.Sequential(
                nn.Linear(feat_dim, waist_size),
                nn.Dropout(dropout_prob),
                nn.Linear(waist_size, hidden_dim),
                nn.Dropout(dropout_prob))
        
        if out_dim > 0:
            self.out = nn.Linear(hidden_dim, out_dim)
        else:
            self.out = None

        # manually set cuda because module doesn't see these combiners for bottom         
        if CUDA:
            self.expand = self.expand.cuda()
            if out_dim > 0:
                self.out = self.out.cuda()

    def forward(self, hidden, feat):
        combined = self.expand(feat) + hidden
    
        if self.out is not None:
            return self.out(combined)

        return combined


class BertForMultitaskWithFeaturesOnTop(PreTrainedBertModel):
    """ stick the features on top of the model """
    def __init__(self, config, cls_num_labels=2, tok_num_labels=2, tok2id=None, args=None):
        super(BertForMultitaskWithFeaturesOnTop, self).__init__(config)
        self.bert = BertModel(config)
        
        self.featurizer = tagging_features.Featurizer(
            tok2id, lexicon_feature_bits=args.lexicon_feature_bits) 
        # TODO -- don't hardcode this...
        nfeats = 126 if args.lexicon_feature_bits == 1 else 154

        if args.extra_features_method == 'concat':
            self.tok_classifier = ConcatCombine(
                config.hidden_size, nfeats, tok_num_labels, 
                args.combiner_layers, config.hidden_dropout_prob,
                args.small_waist)
        else:
            self.tok_classifier = AddCombine(
                nfeats, config.hidden_size, args.combiner_layers,
                config.hidden_dropout_prob, args.small_waist,
                out_dim=tok_num_labels)

        self.cls_dropout = nn.Dropout(config.hidden_dropout_prob)
        self.cls_classifier = nn.Linear(config.hidden_size, cls_num_labels)

        self.apply(self.init_bert_weights)


    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None, rel_ids=None, pos_ids=None):
        features = self.featurizer.featurize_batch(
            input_ids.detach().cpu().numpy(), 
            rel_ids.detach().cpu().numpy(), 
            pos_ids.detach().cpu().numpy(), 
            padded_len=input_ids.shape[1])
        features = torch.tensor(features, dtype=torch.float)
        if CUDA:
            features = features.cuda()
            
        sequence_output, pooled_output, attn_maps = self.bert(
            input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)

        pooled_output = self.cls_dropout(pooled_output)
        cls_logits = self.cls_classifier(pooled_output)

        tok_logits = self.tok_classifier(sequence_output, features)

        return cls_logits, tok_logits


class BertForMultitaskWithFeaturesOnBottom(PreTrainedBertModel):
    """ stick the features on top of the model """
    def __init__(self, config, cls_num_labels=2, tok_num_labels=2, tok2id=None, args=None):
        super(BertForMultitaskWithFeaturesOnBottom, self).__init__(config)
        
        self.featurizer = tagging_features.Featurizer(
            tok2id, lexicon_feature_bits=args.lexicon_feature_bits) 
        # TODO -- don't hardcode this...
        nfeats = 126 if args.lexicon_feature_bits == 1 else 154

        if args.extra_features_method == 'concat':
            if ARGS.share_combiners:
                self.combiners = {
                    i: ConcatCombine(
                        config.hidden_size, nfeats, config.hidden_size, 
                        args.combiner_layers, config.hidden_dropout_prob,
                        args.small_waist)
                    for i in range(1, 7)
                }
            else:
                combiner = ConcatCombine(
                    config.hidden_size, nfeats, config.hidden_size, 
                    args.combiner_layers, config.hidden_dropout_prob,
                    args.small_waist)
                self.combiners = { i: combiner for i in range(1, 7) }
        else:
            if ARGS.share_combiners:
                self.combiners = {
                    i: AddCombine(
                nfeats, config.hidden_size, args.combiner_layers,
                config.hidden_dropout_prob, args.small_waist)
                    for i in range(1, 7)
                }
            else:
                combiner = AddCombine(
                    nfeats, config.hidden_size, args.combiner_layers,
                    config.hidden_dropout_prob, args.small_waist)
                self.combiners = { i: combiner for i in range(1, 7) }

        self.bert = BertModelBottomFeatures(config, self.combiners)

        self.cls_dropout = nn.Dropout(config.hidden_dropout_prob)
        self.cls_classifier = nn.Linear(config.hidden_size, cls_num_labels)

        self.tok_dropout = nn.Dropout(config.hidden_dropout_prob)
        self.tok_classifier = nn.Linear(config.hidden_size, tok_num_labels)

        self.apply(self.init_bert_weights)


    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None, rel_ids=None, pos_ids=None):
        features = self.featurizer.featurize_batch(
            input_ids.detach().cpu().numpy(),
            rel_ids.detach().cpu().numpy(),
            pos_ids.detach().cpu().numpy(),
            padded_len=input_ids.shape[1])
        features = torch.tensor(features, dtype=torch.float)
        if CUDA:
            features = features.cuda()
        
        sequence_output, pooled_output, attn_maps = self.bert(
            input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False,
            features=features)

        sequence_output = self.cls_dropout(sequence_output)
        cls_logits = self.cls_classifier(pooled_output)

        # NOTE -- dropout is after proj, which is non-standard
        #      -- switch back if nessicary!
        tok_logits = self.tok_classifier(sequence_output)
        tok_logits = self.tok_dropout(tok_logits)

        return cls_logits, tok_logits











class BertModelBottomFeatures(BertModel):
    def __init__(self, config, combiners):
        super(BertModelBottomFeatures, self).__init__(config)
        self.embeddings = modeling.BertEmbeddings(config)
        self.encoder = BertEncoderF(config, combiners)
        self.pooler = modeling.BertPooler(config)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, output_all_encoded_layers=True, features=None):
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0

        embedding_output = self.embeddings(input_ids, token_type_ids)
        encoded_layers, layerwise_attn_probs = self.encoder(embedding_output,
                                      extended_attention_mask,
                                      output_all_encoded_layers=output_all_encoded_layers,
                                      features=features)
        sequence_output = encoded_layers[-1]
        pooled_output = self.pooler(sequence_output)
        if not output_all_encoded_layers:
            encoded_layers = encoded_layers[-1]
        return encoded_layers, pooled_output, layerwise_attn_probs




class BertSelfOutputF(nn.Module):
    def __init__(self, config, combiners):
        super(BertSelfOutputF, self).__init__()
        self.combiners = combiners
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = modeling.BertLayerNorm(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor, features=None):
        global ARGS
        
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)

        ### COMBINE1
        if features is not None and ARGS.combine1:
            hidden_states = self.combiners[1](hidden_states, features)

        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertAttentionF(nn.Module):
    def __init__(self, config, combiners):
        super(BertAttentionF, self).__init__()
        self.combiners = combiners
        self.self = modeling.BertSelfAttention(config)
        self.output = BertSelfOutputF(config, combiners)

    def forward(self, input_tensor, attention_mask, features=None):
        self_output, attn_probs = self.self(input_tensor, attention_mask)

        ### COMBINE2
        if features is not None and ARGS.combine2:
            self_output = self.combiners[2](self_output, features)

        attention_output = self.output(self_output, input_tensor, features=features)
        return attention_output, attn_probs


class BertOutputF(nn.Module):
    def __init__(self, config, combiners):
        super(BertOutputF, self).__init__()
        self.combiners = combiners
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.LayerNorm = modeling.BertLayerNorm(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor, features=None):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)

        ### COMBINE3
        if features is not None and ARGS.combine3:
            hidden_states = self.combiners[3](hidden_states, features)

        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertLayerF(nn.Module):
    def __init__(self, config, combiners):
        super(BertLayerF, self).__init__()
        self.combiners = combiners
        self.attention = BertAttentionF(config, combiners)
        self.intermediate = modeling.BertIntermediate(config)
        self.output = BertOutputF(config, combiners)

    def forward(self, hidden_states, attention_mask, features=None):

        ### COMBINE4        
        if features is not None and ARGS.combine4:
            hidden_states = self.combiners[4](hidden_states, features)

        attention_output, attn_probs = self.attention(hidden_states, attention_mask, features=features)
        
        ### COMBINE5
        if features is not None and ARGS.combine5:
            hidden_states = self.combiners[5](hidden_states, features)

        intermediate_output = self.intermediate(attention_output)
        
        ### COMBINE6
        if features is not None and ARGS.combine4:
            hidden_states = self.combiners[6](hidden_states, features)

        layer_output = self.output(intermediate_output, attention_output, features=features)
        return layer_output, attn_probs


class BertEncoderF(nn.Module):
    def __init__(self, config, combiners):
        super(BertEncoderF, self).__init__()
        layer = BertLayerF(config, combiners)
        self.layer = nn.ModuleList([copy.deepcopy(layer) for _ in range(config.num_hidden_layers)])    

    def forward(self, hidden_states, attention_mask, output_all_encoded_layers=True, features=None):
        all_encoder_layers = []
        all_layer_attns = []
        for i, layer_module in enumerate(self.layer):
            if i == len(self.layer) - 1:
                hidden_states, attn_probs = layer_module(hidden_states, attention_mask, features=features)
            else:
                hidden_states, attn_probs = layer_module(hidden_states, attention_mask)

            all_layer_attns.append(attn_probs)
            if output_all_encoded_layers:
                all_encoder_layers.append(hidden_states)
        if not output_all_encoded_layers:
            all_encoder_layers.append(hidden_states)
        return all_encoder_layers, all_layer_attns



